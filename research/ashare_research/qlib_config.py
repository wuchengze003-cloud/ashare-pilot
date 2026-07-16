"""Shared Qlib runtime configuration compatible with MLflow 3."""

from pathlib import Path


def sqlite_exp_manager(database_path: Path | str) -> dict:
    uri = f"sqlite:///{Path(database_path).resolve()}"
    return {
        "class": "MLflowExpManager",
        "module_path": "qlib.workflow.expm",
        "kwargs": {
            "uri": uri,
            "default_exp_name": "ashare-research",
        },
    }
