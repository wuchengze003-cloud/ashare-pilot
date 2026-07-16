#!/usr/bin/env python3
"""Run bounded agent jobs and verify their filesystem scope."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = REPO_ROOT / "ops" / "agents" / "runtime"
WORKTREE_ROOT = REPO_ROOT / ".agent-worktrees"


def run(command: list[str], cwd: Path, timeout: int, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def git_paths(cwd: Path) -> list[str]:
    result = run(["git", "status", "--porcelain", "--untracked-files=all"], cwd, 30)
    paths: list[str] = []
    for line in result.stdout.splitlines():
        raw = line[3:]
        if " -> " in raw:
            raw = raw.split(" -> ", 1)[1]
        paths.append(raw.strip('"'))
    return sorted(paths)


def repo_fingerprint(cwd: Path) -> str:
    digest = hashlib.sha256()
    diff = run(["git", "diff", "--binary", "HEAD"], cwd, 60)
    digest.update(diff.stdout.encode())
    for path in git_paths(cwd):
        file = cwd / path
        if file.is_file() and run(["git", "ls-files", "--error-unmatch", path], cwd, 30, check=False).returncode != 0:
            digest.update(path.encode())
            digest.update(file.read_bytes())
    return digest.hexdigest()


def protected_fingerprint(cwd: Path, paths: list[str]) -> str:
    """Detect accidental writes to protected tracked and ignored paths."""
    digest = hashlib.sha256()
    for relative in sorted(paths):
        target = cwd / relative
        digest.update(relative.encode())
        if not target.exists():
            digest.update(b"missing")
            continue
        candidates = [target] if target.is_file() else sorted(target.rglob("*"))
        for candidate in candidates:
            if not candidate.is_file():
                continue
            stat = candidate.stat()
            digest.update(str(candidate.relative_to(cwd)).encode())
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()


def path_allowed(path: str, allowed: list[str], forbidden: list[str]) -> bool:
    normalized = path.replace("\\", "/")
    if any(normalized == item or normalized.startswith(f"{item.rstrip('/')}/") for item in forbidden):
        return False
    return any(normalized == item or normalized.startswith(f"{item.rstrip('/')}/") for item in allowed)


def agent_command(agent: str, prompt: str, read_only: bool) -> list[str]:
    if agent == "hermes":
        return ["hermes", "-z", prompt]
    if agent == "gemini":
        mode = "plan" if read_only else "auto_edit"
        return ["gemini", "-p", prompt, "--approval-mode", mode, "-o", "json"]
    if agent == "claude":
        mode = "plan" if read_only else "acceptEdits"
        return ["claude", "-p", prompt, "--permission-mode", mode, "--output-format", "json"]
    raise ValueError(f"unsupported agent: {agent}")


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = json.loads(path.read_text("utf-8"))
    required = {
        "id",
        "agent",
        "mode",
        "prompt",
        "allowed_paths",
        "forbidden_paths",
        "input_data_cutoff",
        "expected_outputs",
        "tests",
    }
    missing = required - manifest.keys()
    if missing:
        raise ValueError(f"manifest missing fields: {sorted(missing)}")
    if manifest["mode"] not in {"read_only", "worktree_code"}:
        raise ValueError("mode must be read_only or worktree_code")
    if int(manifest.get("max_subagents", 0)) > 3:
        raise ValueError("max_subagents cannot exceed 3")
    if "required_verdict" in manifest and not str(manifest["required_verdict"]).strip():
        raise ValueError("required_verdict cannot be blank")
    return manifest


def dispatch(manifest_path: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    started = time.time()
    timeout = int(manifest.get("timeout_seconds", 1800))
    read_only = manifest["mode"] == "read_only"
    cwd = REPO_ROOT
    branch: str | None = None
    worktree: Path | None = None

    prompt = (
        f"{manifest['prompt']}\n\n"
        f"Input data cutoff: {manifest['input_data_cutoff']}\n"
        f"Expected outputs: {manifest['expected_outputs']}\n"
        f"Allowed paths: {manifest['allowed_paths']}\n"
        f"Forbidden paths: {manifest['forbidden_paths']}\n"
        f"Maximum subagents: {manifest.get('max_subagents', 0)}\n"
        "Return a concise result with evidence and commands run."
    )

    if read_only:
        before = repo_fingerprint(REPO_ROOT)
        protected_before = protected_fingerprint(REPO_ROOT, manifest["forbidden_paths"])
    else:
        dirty = git_paths(REPO_ROOT)
        relevant_dirty = [
            path
            for path in dirty
            if path_allowed(path, manifest["allowed_paths"], manifest["forbidden_paths"])
        ]
        if relevant_dirty:
            raise RuntimeError(
                "coding agent requires a clean, reviewed HEAD; "
                f"allowed paths have uncommitted changes: {relevant_dirty}"
            )
        stamp = time.strftime("%Y%m%d-%H%M%S")
        branch = f"agent/{manifest['agent']}/{manifest['id']}-{stamp}"
        worktree = WORKTREE_ROOT / f"{manifest['id']}-{stamp}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "worktree", "add", "-b", branch, str(worktree), "HEAD"], REPO_ROOT, 120)
        cwd = worktree

    process = run(agent_command(manifest["agent"], prompt, read_only), cwd, timeout, check=False)
    changed = git_paths(cwd)

    if read_only:
        if repo_fingerprint(REPO_ROOT) != before:
            raise RuntimeError("read-only agent changed repository state")
        if protected_fingerprint(REPO_ROOT, manifest["forbidden_paths"]) != protected_before:
            raise RuntimeError("read-only agent changed a protected runtime path")
    else:
        invalid = [
            path for path in changed
            if not path_allowed(path, manifest["allowed_paths"], manifest["forbidden_paths"])
        ]
        if invalid:
            raise RuntimeError(f"agent changed paths outside scope: {invalid}")

    tests: list[dict[str, Any]] = []
    if process.returncode == 0:
        for command in manifest.get("tests", []):
            test = run(["zsh", "-lc", command], cwd, timeout, check=False)
            tests.append({
                "command": command,
                "returncode": test.returncode,
                "stdout": test.stdout[-4000:],
                "stderr": test.stderr[-4000:],
            })

    required_verdict = manifest.get("required_verdict")
    first_line = next(
        (line.strip() for line in process.stdout.splitlines() if line.strip()),
        "",
    )
    verdict_passed = (
        required_verdict is None
        or first_line.upper().startswith(str(required_verdict).strip().upper())
    )

    result = {
        "id": manifest["id"],
        "agent": manifest["agent"],
        "mode": manifest["mode"],
        "status": (
            "passed"
            if process.returncode == 0
            and all(t["returncode"] == 0 for t in tests)
            and verdict_passed
            else "failed"
        ),
        "required_verdict": required_verdict,
        "agent_verdict": first_line,
        "branch": branch,
        "worktree": str(worktree) if worktree else None,
        "changed_paths": changed,
        "input_data_cutoff": manifest["input_data_cutoff"],
        "expected_outputs": manifest["expected_outputs"],
        "duration_seconds": round(time.time() - started, 2),
        "stdout": process.stdout[-12000:],
        "stderr": process.stderr[-12000:],
        "tests": tests,
    }
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    output = RUNTIME_DIR / f"{manifest['id']}-{int(started)}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    result["result_file"] = str(output)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    print(json.dumps(dispatch(args.manifest.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
