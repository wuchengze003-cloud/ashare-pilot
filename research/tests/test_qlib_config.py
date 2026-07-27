from ashare_research.qlib_config import sqlite_exp_manager


def test_mlflow_uses_sqlite_backend(tmp_path):
    config = sqlite_exp_manager(tmp_path / "mlflow.db")
    assert config["kwargs"]["uri"].startswith("sqlite:///")
    assert config["kwargs"]["default_exp_name"] == "ashare-research"
