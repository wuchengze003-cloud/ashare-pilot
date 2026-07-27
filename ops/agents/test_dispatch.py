"""Unit tests for ops/agents/dispatch.py security guardrails."""

import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dispatch import agent_command, load_manifest, path_allowed


class TestPathAllowed(unittest.TestCase):
    """path_allowed is the filesystem scope gate for agent jobs."""

    def test_exact_match_allowed(self):
        self.assertTrue(path_allowed("web/lib/foo.ts", ["web/lib/foo.ts"], []))

    def test_prefix_match_allowed(self):
        self.assertTrue(path_allowed("web/lib/sub/bar.ts", ["web/lib"], []))

    def test_not_in_allowed(self):
        self.assertFalse(path_allowed("pyserver/main.py", ["web/lib"], []))

    def test_forbidden_overrides_allowed(self):
        self.assertFalse(
            path_allowed("web/data/universe.json", ["web"], ["web/data/universe.json"])
        )

    def test_forbidden_directory_prefix(self):
        self.assertFalse(
            path_allowed("web/data/runtime/signals.json", ["web"], ["web/data/runtime"])
        )

    def test_partial_name_not_matched(self):
        # "web/lib2" should NOT match allowed "web/lib"
        self.assertFalse(path_allowed("web/lib2/x.ts", ["web/lib"], []))

    def test_backslash_normalized(self):
        self.assertTrue(path_allowed("web\\lib\\foo.ts", ["web/lib"], []))

    def test_trailing_slash_in_allowed(self):
        self.assertTrue(path_allowed("web/lib/foo.ts", ["web/lib/"], []))

    def test_empty_allowed_denies_all(self):
        self.assertFalse(path_allowed("anything.ts", [], []))

    def test_forbidden_exact_file(self):
        self.assertFalse(
            path_allowed("active_model.json", ["research"], ["active_model.json"])
        )


class TestLoadManifest(unittest.TestCase):
    """load_manifest validates the declarative job contract."""

    VALID = {
        "id": "test-job",
        "agent": "hermes",
        "mode": "read_only",
        "prompt": "do something",
        "allowed_paths": ["reports/"],
        "forbidden_paths": ["web/data/"],
        "input_data_cutoff": "2026-07-01",
        "expected_outputs": ["reports/out.json"],
        "tests": ["echo ok"],
    }

    def _write(self, data: dict) -> Path:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(data, f)
        f.close()
        return Path(f.name)

    def test_valid_manifest_loads(self):
        m = load_manifest(self._write(self.VALID))
        self.assertEqual(m["id"], "test-job")

    def test_missing_field_raises(self):
        bad = {k: v for k, v in self.VALID.items() if k != "tests"}
        with self.assertRaises(ValueError) as ctx:
            load_manifest(self._write(bad))
        self.assertIn("tests", str(ctx.exception))

    def test_invalid_mode_raises(self):
        bad = {**self.VALID, "mode": "yolo"}
        with self.assertRaises(ValueError):
            load_manifest(self._write(bad))

    def test_max_subagents_exceeds_3_raises(self):
        bad = {**self.VALID, "max_subagents": 5}
        with self.assertRaises(ValueError):
            load_manifest(self._write(bad))

    def test_blank_required_verdict_raises(self):
        bad = {**self.VALID, "required_verdict": "  "}
        with self.assertRaises(ValueError):
            load_manifest(self._write(bad))

    def test_worktree_code_mode_accepted(self):
        m = load_manifest(self._write({**self.VALID, "mode": "worktree_code"}))
        self.assertEqual(m["mode"], "worktree_code")


class TestAgentCommand(unittest.TestCase):
    """agent_command maps agent names to CLI invocations."""

    def test_hermes(self):
        cmd = agent_command("hermes", "hello", read_only=True)
        self.assertEqual(cmd[0], "hermes")
        self.assertIn("hello", cmd)

    def test_gemini_read_only(self):
        cmd = agent_command("gemini", "hi", read_only=True)
        self.assertIn("plan", cmd)

    def test_gemini_edit(self):
        cmd = agent_command("gemini", "hi", read_only=False)
        self.assertIn("auto_edit", cmd)

    def test_claude_read_only(self):
        cmd = agent_command("claude", "hi", read_only=True)
        self.assertIn("plan", cmd)

    def test_claude_edit(self):
        cmd = agent_command("claude", "hi", read_only=False)
        self.assertIn("acceptEdits", cmd)

    def test_unknown_agent_raises(self):
        with self.assertRaises(ValueError):
            agent_command("skynet", "hi", read_only=True)


if __name__ == "__main__":
    unittest.main()
