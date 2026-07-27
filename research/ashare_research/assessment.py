"""Build promotion evidence and assess a registered shadow challenger."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .candidate import build_promotion_evidence, write_promotion_evidence
from .contracts import ModelMetrics
from .evaluation import file_sha256
from .promotion import evaluate_promotion
from .registry import (
    RegistryError,
    load_model_manifest,
    load_registry,
    promote,
    verify_promotion_evidence,
)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        "utf-8",
    )


def _champion_sources(runtime: Path, registry: dict) -> tuple[Path, Path]:
    active = registry.get("active")
    if not active:
        return (
            runtime / "baselines" / "v1" / "evaluation.json",
            runtime / "baselines" / "v1" / "metrics.json",
        )
    evaluation = Path(active["champion_evaluation_uri"])
    metrics = Path(active["champion_metrics_uri"])
    expected = {
        evaluation: active["champion_evaluation_sha256"],
        metrics: active["champion_metrics_sha256"],
    }
    for path, digest in expected.items():
        if not path.exists() or file_sha256(path) != digest:
            raise RegistryError(f"active champion evidence hash mismatch: {path}")
    return evaluation, metrics


def _metrics(path: Path) -> ModelMetrics:
    return ModelMetrics(**json.loads(path.read_text("utf-8")))


def _not_ready(model_version: str, assessment_path: Path, failures: list[dict]) -> dict:
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model_version": model_version,
        "status": "shadow-not-ready",
        "passed": False,
        "failures": failures,
    }
    _write(assessment_path, result)
    return result


def assess_registered_challenger(
    runtime_root: Path | str,
    model_version: str,
    auto_promote: bool = False,
) -> dict:
    runtime = Path(runtime_root)
    registry_root = runtime / "registry"
    registry = load_registry(registry_root)
    manifest = load_model_manifest(registry_root, model_version)
    evaluation_dir = runtime / "evaluations" / model_version
    assessment_path = evaluation_dir / "promotion-assessment.json"
    required = {
        "shadow": evaluation_dir / "shadow.json",
        "quality": runtime / "quality" / "feature-panel.json",
        "drift": runtime / "drift" / "latest.json",
    }
    missing = [name for name, path in required.items() if not path.exists()]
    if missing:
        return _not_ready(
            model_version,
            assessment_path,
            [
                {"code": "MISSING_EVIDENCE", "actual": name, "required": "existing report"}
                for name in missing
            ],
        )

    champion_evaluation, champion_metrics = _champion_sources(runtime, registry)
    missing_champion = [path for path in (champion_evaluation, champion_metrics) if not path.exists()]
    if missing_champion:
        return _not_ready(
            model_version,
            assessment_path,
            [
                {
                    "code": "MISSING_CHAMPION_BASELINE",
                    "actual": str(path),
                    "required": "current champion evaluation",
                }
                for path in missing_champion
            ],
        )

    evidence = build_promotion_evidence(
        manifest,
        manifest.oos_evaluation_uri,
        manifest.holdout_evaluation_uri,
        required["shadow"],
        champion_evaluation,
        champion_metrics,
        required["quality"],
        required["drift"],
    )
    evidence_path = evaluation_dir / "promotion-evidence.json"
    write_promotion_evidence(evidence, evidence_path)
    verified_champion = verify_promotion_evidence(manifest, evidence)
    if verified_champion != _metrics(champion_metrics):
        raise RegistryError("verified champion metrics differ from source")
    gate = evaluate_promotion(manifest, evidence, verified_champion)
    status = "eligible" if gate.passed else "shadow-not-ready"
    if gate.passed and auto_promote:
        promote(registry_root, manifest, evidence)
        status = "promoted"
    result = {
        "generated_at": datetime.now(UTC).isoformat(),
        "model_version": model_version,
        "status": status,
        "passed": gate.passed,
        "auto_promote": auto_promote,
        "metrics": asdict(evidence.metrics),
        "failures": [asdict(failure) for failure in gate.failures],
        "evidence_path": str(evidence_path.resolve()),
    }
    _write(assessment_path, result)
    return result
