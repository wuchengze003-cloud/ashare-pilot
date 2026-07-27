"""Atomic model registry with auditable promotion and rollback."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .candidate import build_promotion_evidence
from .contracts import ModelManifest, ModelMetrics, PromotionEvidence
from .evaluation import file_sha256
from .promotion import evaluate_promotion


class RegistryError(RuntimeError):
    pass


def _state_path(root: Path | str) -> Path:
    return Path(root) / "active_model.json"


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def load_registry(root: Path | str) -> dict[str, Any]:
    path = _state_path(root)
    if not path.exists():
        return {
            "schema_version": 1,
            "active": None,
            "history": [],
            "candidates": [],
            "retired": [],
        }
    state = json.loads(path.read_text("utf-8"))
    state.setdefault("history", [])
    state.setdefault("candidates", [])
    state.setdefault("retired", [])
    return state


def _resolve_artifact(root: Path | str, uri: str) -> Path:
    if uri.startswith("models:/"):
        version = uri.removeprefix("models:/").strip("/")
        return Path(root).parent / "models" / version / "model-bundle.pkl"
    return Path(uri)


def _verify_manifest(root: Path | str, manifest: ModelManifest) -> None:
    sources = (
        ("artifact", _resolve_artifact(root, manifest.artifact_uri), manifest.artifact_sha256),
        ("OOS evaluation", Path(manifest.oos_evaluation_uri), manifest.oos_evaluation_sha256),
        (
            "holdout evaluation",
            Path(manifest.holdout_evaluation_uri),
            manifest.holdout_evaluation_sha256,
        ),
    )
    for label, path, expected in sources:
        if not expected:
            raise RegistryError(f"{label} hash is required")
        if not path.exists():
            raise RegistryError(f"{label} missing: {path}")
        actual = file_sha256(path)
        if actual != expected:
            raise RegistryError(f"{label} hash mismatch")


def register_candidate(root: Path | str, manifest: ModelManifest) -> Path:
    _verify_manifest(root, manifest)
    model_dir = Path(root) / "models" / manifest.model_version
    manifest_path = model_dir / "manifest.json"
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text("utf-8"))
        normalized = json.loads(json.dumps(asdict(manifest), default=str))
        if existing != normalized:
            raise RegistryError(f"immutable manifest conflict: {manifest.model_version}")
        return manifest_path
    _atomic_json(manifest_path, asdict(manifest))
    state = load_registry(root)
    if manifest.model_version not in state["candidates"]:
        state["candidates"].append(manifest.model_version)
    _atomic_json(_state_path(root), state)
    return manifest_path


def load_model_manifest(root: Path | str, model_version: str) -> ModelManifest:
    path = Path(root) / "models" / model_version / "manifest.json"
    if not path.exists():
        raise RegistryError(f"candidate manifest missing: {path}")
    value = json.loads(path.read_text("utf-8"))
    return ModelManifest(
        model_version=value["model_version"],
        feature_version=value["feature_version"],
        model_type=value["model_type"],
        stage=value["stage"],
        created_at=datetime.fromisoformat(value["created_at"]),
        data_cutoff=date.fromisoformat(value["data_cutoff"]),
        artifact_uri=value["artifact_uri"],
        primary_window=value["primary_window"],
        data_source=value["data_source"],
        promotable=bool(value["promotable"]),
        artifact_sha256=value["artifact_sha256"],
        oos_evaluation_uri=value["oos_evaluation_uri"],
        oos_evaluation_sha256=value["oos_evaluation_sha256"],
        holdout_evaluation_uri=value["holdout_evaluation_uri"],
        holdout_evaluation_sha256=value["holdout_evaluation_sha256"],
        training_windows=tuple(value.get("training_windows", [])),
        params=value.get("params", {}),
    )


def verify_model_artifacts(root: Path | str, model_version: str) -> ModelManifest:
    manifest = load_model_manifest(root, model_version)
    _verify_manifest(root, manifest)
    return manifest


def verify_promotion_evidence(
    manifest: ModelManifest,
    evidence: PromotionEvidence,
) -> ModelMetrics:
    required = {
        "oos",
        "holdout",
        "shadow",
        "champion",
        "champion_metrics",
        "quality",
        "drift",
    }
    if set(evidence.source_uris) != required or set(evidence.source_hashes) != required:
        raise RegistryError("promotion evidence sources are incomplete")
    paths = {name: Path(uri) for name, uri in evidence.source_uris.items()}
    for name, path in paths.items():
        if not path.exists():
            raise RegistryError(f"promotion evidence source missing: {name}={path}")
        if file_sha256(path) != evidence.source_hashes[name]:
            raise RegistryError(f"promotion evidence source hash mismatch: {name}")
    if paths["oos"].resolve() != Path(manifest.oos_evaluation_uri).resolve():
        raise RegistryError("promotion evidence OOS source does not match manifest")
    if paths["holdout"].resolve() != Path(manifest.holdout_evaluation_uri).resolve():
        raise RegistryError("promotion evidence holdout source does not match manifest")
    rebuilt = build_promotion_evidence(
        manifest,
        paths["oos"],
        paths["holdout"],
        paths["shadow"],
        paths["champion"],
        paths["champion_metrics"],
        paths["quality"],
        paths["drift"],
    )
    if rebuilt.metrics != evidence.metrics:
        raise RegistryError("promotion metrics do not match system evaluation")
    try:
        champion_payload = json.loads(paths["champion_metrics"].read_text("utf-8"))
        champion = ModelMetrics(**champion_payload)
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise RegistryError("champion metrics source is invalid") from error
    return champion


def promote(
    root: Path | str,
    manifest: ModelManifest,
    evidence: PromotionEvidence,
) -> dict[str, Any]:
    champion = verify_promotion_evidence(manifest, evidence)
    gate = evaluate_promotion(manifest, evidence, champion)
    if not gate.passed:
        failed = ",".join(failure.code for failure in gate.failures)
        raise RegistryError(f"promotion gate failed: {failed}")
    register_candidate(root, manifest)
    model_dir = Path(root) / "models" / manifest.model_version
    evidence_path = model_dir / "promotion-evidence.json"
    metrics_path = model_dir / "champion-metrics.json"
    _atomic_json(evidence_path, asdict(evidence))
    _atomic_json(metrics_path, asdict(evidence.metrics))
    state = load_registry(root)
    if state["active"]:
        state["history"].append(state["active"])
    state["active"] = {
        "model_version": manifest.model_version,
        "feature_version": manifest.feature_version,
        "model_type": manifest.model_type,
        "data_source": manifest.data_source,
        "promotable": manifest.promotable,
        "artifact_uri": manifest.artifact_uri,
        "champion_evaluation_uri": manifest.holdout_evaluation_uri,
        "champion_evaluation_sha256": manifest.holdout_evaluation_sha256,
        "champion_metrics_uri": str(metrics_path.resolve()),
        "champion_metrics_sha256": file_sha256(metrics_path),
        "promotion_evidence_uri": str(evidence_path.resolve()),
        "promotion_evidence_sha256": file_sha256(evidence_path),
        "data_cutoff": manifest.data_cutoff.isoformat(),
        "promoted_at": datetime.now(UTC).isoformat(),
        "validated_max_drawdown_pct": evidence.metrics.max_drawdown_pct,
        "rollback_drawdown_limit_pct": min(
            15.0, max(abs(evidence.metrics.max_drawdown_pct) * 1.25, 1.0)
        ),
        "promotion_evidence": asdict(evidence),
    }
    state["candidates"] = [
        version for version in state["candidates"] if version != manifest.model_version
    ]
    _atomic_json(_state_path(root), state)
    return state["active"]


def rollback(root: Path | str, reason: str) -> dict[str, Any]:
    state = load_registry(root)
    current = state["active"]
    if not current:
        raise RegistryError("no active model to roll back")
    retired = {
        **current,
        "retired_at": datetime.now(UTC).isoformat(),
        "retirement_reason": reason,
    }
    state["retired"].append(retired)
    if state["history"]:
        restored = state["history"].pop()
        state["active"] = {
            **restored,
            "promoted_at": datetime.now(UTC).isoformat(),
            "rollback_reason": reason,
            "rolled_back_from": current["model_version"],
        }
        result = state["active"]
    else:
        state["active"] = None
        result = {
            "model_version": "V1",
            "strategy": "dashboard-rule",
            "rollback_reason": reason,
            "rolled_back_from": current["model_version"],
        }
    _atomic_json(_state_path(root), state)
    return result
