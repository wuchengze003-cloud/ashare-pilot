"""Rebound study report generation, config locking, and study orchestration.

Implements §M5 of the development plan:
- Config lock with SHA-256 verification
- Run manifest with git state, config hash, data coverage
- Markdown report generation (Chinese, distinguishing fact/assumption/result)
- Study orchestration: development → validation → lock → frozen
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from .minute_execution import ExecutionConfig, TradeResult, simulate_trade
from .minute_quality import QUALITY_REPORT_VERSION
from .rebound_study import (
    DEV_END,
    DEV_START,
    ENTRY_STRATEGIES,
    FROZEN_START,
    HOLD_PERIODS,
    MIN_FROZEN_EVENTS,
    MIN_FROZEN_TRADING_DAYS,
    PORTFOLIO_CAPITAL,
    POSITION_CAP_PCT,
    RISK_BUDGET_PCT,
    STOP_LOSS_PCT,
    VAL_END,
    VAL_START,
    EntryRule,
    ReboundEvent,
    StrategyResult,
    StudySummary,
    compute_strategy_stats,
    detect_events,
    hash_config,
    load_rebound_config,
    load_universe_membership,
    select_strategy,
)

CONFIG_LOCK_VERSION = 3

# ---------------------------------------------------------------------------
# Git state
# ---------------------------------------------------------------------------


def _git_state(repo_root: Path) -> dict[str, Any]:
    """Capture current git commit and dirty state."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root, capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return {"commit": commit, "dirty": bool(dirty), "dirty_files": len(dirty.splitlines()) if dirty else 0}
    except Exception:
        return {"commit": "unknown", "dirty": None, "dirty_files": 0}


# ---------------------------------------------------------------------------
# Config lock
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _parse_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"non-finite JSON number is not allowed: {value}")
    return number


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise ValueError(f"{label} not found: {path}")
    try:
        payload = json.loads(
            path.read_text("utf-8"),
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"{label} is not valid JSON: {path}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return payload


def _strategy_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        f"{item.get('strategy_name', '')}_{item.get('hold_days', 0)}d": item
        for item in report.get("strategies", [])
        if isinstance(item, dict)
    }


def _validate_lock_sources(
    config_path: Path,
    coverage_path: Path,
    daily_coverage_path: Path,
    universe_path: Path,
    selected_strategy: str,
    dev_path: Path,
    val_path: Path,
) -> tuple[str, dict[str, Any], dict[str, Any], str, str]:
    config = load_rebound_config(config_path)
    config_hash = hash_config(config)

    universe_membership = load_universe_membership(universe_path)
    universe_hash = _sha256_file(universe_path)
    coverage, coverage_error = _load_minute_coverage(
        coverage_path,
        str(config.get("freq", "5min")),
        universe_membership,
    )
    if coverage_error:
        raise ValueError(f"coverage report cannot be locked: {coverage_error}")
    if coverage is None:
        raise ValueError("coverage report cannot be locked")
    if not selected_strategy:
        raise ValueError("selected_strategy is required")
    development_start = str(
        config["data_window"]["development"]["start"]
    ).replace("-", "")
    validation_end = str(
        config["data_window"]["validation"]["end"]
    ).replace("-", "")
    coverage_start = str(coverage["start_date"]).replace("-", "")
    coverage_end = str(coverage["end_date"]).replace("-", "")
    if coverage_start > development_start or coverage_end < validation_end:
        raise ValueError(
            "coverage report does not span development through validation"
        )
    history_start = _required_history_start(
        daily_coverage_path.parent.parent,
        development_start,
        int(config["event_thresholds"]["min_listing_days"]),
    )
    daily_coverage, daily_coverage_error = _load_daily_coverage(
        daily_coverage_path,
        history_start,
        validation_end,
    )
    if daily_coverage_error:
        raise ValueError(
            f"daily coverage report cannot be locked: {daily_coverage_error}"
        )
    assert daily_coverage is not None
    daily_coverage_hash = str(daily_coverage["coverage_sha256"])
    strategy_order = [
        f"{entry['name']}_{hold_days}d"
        for entry in config["entry_strategies"]
        for hold_days in config["hold_periods"]
    ]
    allowed_strategies = set(strategy_order)
    if selected_strategy not in allowed_strategies:
        raise ValueError("selected_strategy is not pre-registered")

    dev = _read_json_object(dev_path, "development report")
    val = _read_json_object(val_path, "validation report")
    if dev.get("stage") != "development" or dev.get("verdict") != "development_complete":
        raise ValueError("development report is not a completed development run")
    if val.get("stage") != "validation" or val.get("verdict") != "viable":
        raise ValueError("validation report is not a viable validation run")
    if dev.get("config_hash") != config_hash or val.get("config_hash") != config_hash:
        raise ValueError("development/validation report config hash mismatch")
    if val.get("selected_strategy") != selected_strategy:
        raise ValueError("selected_strategy does not match validation report")
    if not isinstance(dev.get("strategies"), list) or not isinstance(
        val.get("strategies"), list
    ):
        raise ValueError("development/validation strategies must be lists")
    if len(_strategy_map(dev)) != len(dev["strategies"]):
        raise ValueError("development report contains duplicate strategy keys")
    if len(_strategy_map(val)) != len(val["strategies"]):
        raise ValueError("validation report contains duplicate strategy keys")

    dev_map = _strategy_map(dev)
    val_map = _strategy_map(val)
    if set(dev_map) != allowed_strategies:
        raise ValueError("development report strategy set is incomplete")
    if set(val_map) != allowed_strategies:
        raise ValueError("validation report strategy set is incomplete")
    coverage_hash = _sha256_file(coverage_path)
    if dev.get("coverage_sha256") != coverage_hash:
        raise ValueError("development report coverage hash mismatch")
    if val.get("coverage_sha256") != coverage_hash:
        raise ValueError("validation report coverage hash mismatch")
    if dev.get("daily_coverage_sha256") != daily_coverage_hash:
        raise ValueError("development report daily coverage hash mismatch")
    if val.get("daily_coverage_sha256") != daily_coverage_hash:
        raise ValueError("validation report daily coverage hash mismatch")
    if dev.get("universe_sha256") != universe_hash:
        raise ValueError("development report universe hash mismatch")
    if val.get("universe_sha256") != universe_hash:
        raise ValueError("validation report universe hash mismatch")

    numeric_fields = (
        "mean_net_return",
        "expected_profit_per_100k",
        "return_cvar_ratio",
        "max_drawdown",
    )
    for report_label, strategy_map in (
        ("development", dev_map),
        ("validation", val_map),
    ):
        for key, strategy in strategy_map.items():
            filled_trades = strategy.get("filled_trades")
            if (
                isinstance(filled_trades, bool)
                or not isinstance(filled_trades, int)
                or filled_trades < 0
            ):
                raise ValueError(
                    f"{report_label} strategy {key} has invalid filled_trades"
                )
            for field_name in numeric_fields:
                try:
                    value = float(strategy[field_name])
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError(
                        f"{report_label} strategy {key} has invalid {field_name}"
                    ) from error
                if not math.isfinite(value):
                    raise ValueError(
                        f"{report_label} strategy {key} has non-finite {field_name}"
                    )

    dev_strategy = dev_map[selected_strategy]
    val_strategy = val_map[selected_strategy]
    minimum = int(config.get("selection_rules", {}).get("min_val_trades", 30))
    if int(val_strategy.get("filled_trades", 0)) < minimum:
        raise ValueError(
            f"selected strategy has fewer than {minimum} validation trades"
        )
    if float(dev_strategy.get("mean_net_return", 0.0)) <= 0:
        raise ValueError("selected strategy development mean return is not positive")
    if float(val_strategy.get("mean_net_return", 0.0)) <= 0:
        raise ValueError("selected strategy validation mean return is not positive")
    viable = [
        key
        for key in strategy_order
        if float(dev_map[key]["mean_net_return"]) > 0
        and float(val_map[key]["mean_net_return"]) > 0
        and int(val_map[key]["filled_trades"]) >= minimum
    ]
    if not viable:
        raise ValueError("validation report has no viable strategy")
    best_profit = max(
        float(val_map[key]["expected_profit_per_100k"]) for key in viable
    )
    viable = [
        key
        for key in viable
        if abs(
            float(val_map[key]["expected_profit_per_100k"]) - best_profit
        )
        < 1e-8
    ]
    best_cvar_ratio = max(
        float(val_map[key]["return_cvar_ratio"]) for key in viable
    )
    viable = [
        key
        for key in viable
        if abs(float(val_map[key]["return_cvar_ratio"]) - best_cvar_ratio)
        < 1e-8
    ]
    expected_selection = min(
        viable,
        key=lambda key: abs(float(val_map[key]["max_drawdown"])),
    )
    if selected_strategy != expected_selection:
        raise ValueError(
            "selected_strategy does not match the pre-registered ranking"
        )
    return config_hash, dev, val, daily_coverage_hash, universe_hash


def _default_daily_coverage_path(minute_coverage_path: Path) -> Path:
    """Resolve the sibling daily coverage artifact from runtime/minute/meta."""
    try:
        runtime_root = minute_coverage_path.parents[2]
    except IndexError as error:
        raise ValueError(
            "cannot derive daily coverage path from minute coverage path"
        ) from error
    return runtime_root / "data" / "meta" / "coverage.json"


def create_config_lock(
    config_path: Path | str,
    coverage_report_path: Path | str | None,
    selected_strategy: str,
    dev_report_path: str,
    val_report_path: str,
    output_path: Path | str,
    daily_coverage_report_path: Path | str | None = None,
    universe_path: Path | str | None = None,
) -> dict[str, Any]:
    """Create the lock only after all immutable inputs pass admission."""
    if coverage_report_path is None:
        raise ValueError("coverage_report_path is required")
    if universe_path is None:
        raise ValueError("universe_path is required")
    config_path = Path(config_path).resolve()
    coverage_path = Path(coverage_report_path).resolve()
    universe_path = Path(universe_path).resolve()
    daily_coverage_path = (
        Path(daily_coverage_report_path).resolve()
        if daily_coverage_report_path is not None
        else _default_daily_coverage_path(coverage_path).resolve()
    )
    dev_path = Path(dev_report_path).resolve()
    val_path = Path(val_report_path).resolve()
    config_hash, _, _, daily_coverage_hash, universe_hash = _validate_lock_sources(
        config_path,
        coverage_path,
        daily_coverage_path,
        universe_path,
        selected_strategy,
        dev_path,
        val_path,
    )

    lock = {
        "lock_version": CONFIG_LOCK_VERSION,
        "config_sha256": config_hash,
        "coverage_sha256": _sha256_file(coverage_path),
        "coverage_report_path": str(coverage_path),
        "daily_coverage_sha256": daily_coverage_hash,
        "daily_coverage_report_path": str(daily_coverage_path),
        "universe_sha256": universe_hash,
        "universe_path": str(universe_path),
        "selected_strategy": selected_strategy,
        "locked_at": datetime.now(UTC).isoformat(),
        "dev_report_path": str(dev_path),
        "dev_report_sha256": _sha256_file(dev_path),
        "val_report_path": str(val_path),
        "val_report_sha256": _sha256_file(val_path),
    }

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return lock


def verify_config_lock(
    config_path: Path | str,
    lock_path: Path | str,
) -> tuple[bool, str]:
    """Revalidate content, verdicts, selection, and all locked file hashes."""
    lock_path = Path(lock_path)
    if not lock_path.exists():
        return False, "config-lock.json not found"
    try:
        lock = _read_json_object(lock_path, "config lock")
        if lock.get("lock_version") != CONFIG_LOCK_VERSION:
            return False, "unsupported or legacy config lock"
        required = (
            "config_sha256",
            "coverage_sha256",
            "coverage_report_path",
            "daily_coverage_sha256",
            "daily_coverage_report_path",
            "universe_sha256",
            "universe_path",
            "selected_strategy",
            "dev_report_path",
            "dev_report_sha256",
            "val_report_path",
            "val_report_sha256",
        )
        missing = [key for key in required if not lock.get(key)]
        if missing:
            return False, f"config lock is missing fields: {missing}"

        config_path = Path(config_path).resolve()
        coverage_path = Path(lock["coverage_report_path"])
        daily_coverage_path = Path(lock["daily_coverage_report_path"])
        universe_path = Path(lock["universe_path"])
        dev_path = Path(lock["dev_report_path"])
        val_path = Path(lock["val_report_path"])
        (
            config_hash,
            _,
            _,
            daily_coverage_hash,
            universe_hash,
        ) = _validate_lock_sources(
            config_path,
            coverage_path,
            daily_coverage_path,
            universe_path,
            str(lock["selected_strategy"]),
            dev_path,
            val_path,
        )
        if config_hash != lock["config_sha256"]:
            return False, "config hash mismatch"
        if daily_coverage_hash != lock["daily_coverage_sha256"]:
            return False, "daily coverage hash mismatch"
        if universe_hash != lock["universe_sha256"]:
            return False, "universe hash mismatch"
        for label, path, hash_key in (
            ("coverage", coverage_path, "coverage_sha256"),
            (
                "daily coverage",
                daily_coverage_path,
                "daily_coverage_sha256",
            ),
            ("universe", universe_path, "universe_sha256"),
            ("development report", dev_path, "dev_report_sha256"),
            ("validation report", val_path, "val_report_sha256"),
        ):
            if _sha256_file(path) != lock[hash_key]:
                return False, f"{label} hash mismatch"
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
        return False, str(error)

    return True, "config lock and all immutable inputs verified"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def create_manifest(
    repo_root: Path,
    config: dict[str, Any],
    stage: str,
    as_of_date: str,
    coverage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create run manifest."""
    git = _git_state(repo_root)
    return {
        "run_at": datetime.now(UTC).isoformat(),
        "git_commit": git["commit"],
        "git_dirty": git["dirty"],
        "config_sha256": hash_config(config),
        "data_coverage": coverage or {},
        "data_source": "tushare_stk_mins",
        "as_of_date": as_of_date,
        "stage": stage,
        "is_frozen": stage == "frozen",
        "code_version": "v1.1",
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_report_md(
    summary: StudySummary,
    stage: str,
    config: dict[str, Any] | None = None,
) -> str:
    """Generate Chinese markdown report for the study run."""
    config = config or {}
    thresholds = config.get("event_thresholds", {})
    execution = config.get("execution", {})
    exit_rules = config.get("exit_rules", {})
    windows = config.get("data_window", {})
    development = windows.get("development", {})
    validation = windows.get("validation", {})
    lines: list[str] = []
    lines.append(f"# 低位反弹事件研究报告 — {stage} 阶段\n")
    lines.append(f"运行时间：{summary.run_at}\n")
    lines.append(f"配置哈希：`{summary.config_hash[:16]}...`\n")
    lines.append("")

    # 历史事实
    lines.append("## 历史事实\n")
    lines.append("- 数据来源：Tushare stk_mins 历史5分钟K线（未复权）")
    lines.append(
        "- 事件定义："
        f"{thresholds.get('min_listing_days', 60)}日上市龄，"
        f"60日区间位置≤{thresholds.get('max_60d_position_pct', 25.0)}%，"
        f"最高收盘回撤≥{thresholds.get('min_60d_drawdown_pct', 20.0)}%，"
        f"5日收益≤{thresholds.get('max_5d_return_pct', -6.0)}%，"
        f"20日均额≥{float(thresholds.get('min_20d_avg_amount', 50_000_000)) / 10_000:.0f}万元"
    )
    lines.append(
        "- 研究区间：开发集 "
        f"{development.get('start', DEV_START)}~{development.get('end', DEV_END)}，"
        "验证集 "
        f"{validation.get('start', VAL_START)}~{validation.get('end', VAL_END)}"
    )
    lines.append("")

    # 模拟假设
    lines.append("## 模拟假设\n")
    lines.append(
        f"- 交易费用：每侧 {execution.get('fee_bps', 10.0)} bps"
    )
    lines.append(
        f"- 滑点：每侧 {execution.get('slippage_bps', 5.0)} bps"
    )
    lines.append(
        "- 容量限制：单笔不超过执行bar成交额的"
        f"{execution.get('capacity_pct', 1.0)}%"
    )
    lines.append("- T+1：当日买入不可当日卖出")
    lines.append("- 涨跌停：涨停不可买，跌停不可卖")
    lines.append("- 整手：100股向下取整")
    lines.append(
        f"- 止损：入场价下方{exit_rules.get('stop_loss_pct', 5.0)}%"
    )
    lines.append("")

    # 研究结果
    lines.append("## 研究结果\n")
    if summary.strategies:
        lines.append("| 方案 | 事件数 | 成交数 | 胜率 | 平均净收益 | 每万元盈利 | 每10万组合盈利 | 判定 |")
        lines.append("|------|--------|--------|------|-----------|-----------|--------------|------|")
        for s in summary.strategies:
            name = f"{s.strategy_name}_{s.hold_days}d"
            lines.append(
                f"| {name} | {s.events} | {s.filled_trades} | "
                f"{s.win_rate:.1%} | {s.mean_net_return:.4%} | "
                f"¥{s.expected_profit_per_10k:.2f} | ¥{s.expected_profit_per_100k:.2f} | "
                f"{'✓' if s.mean_net_return > 0 else '✗'} |"
            )
    else:
        lines.append("无有效策略结果。")
    lines.append("")

    # 证据不足
    if summary.verdict == "insufficient_evidence":
        lines.append("## 证据不足\n")
        lines.append(f"判定：`insufficient_evidence`。{summary.note}\n")

    # 不能成交样本
    lines.append("## 不能成交样本\n")
    for s in summary.strategies:
        if s.no_fill_count > 0:
            lines.append(f"- {s.strategy_name}_{s.hold_days}d: {s.no_fill_count} 笔无法成交")
            for reason, count in s.no_fill_reasons.items():
                lines.append(f"  - {reason}: {count}")
    if all(s.no_fill_count == 0 for s in summary.strategies):
        lines.append("- 无")
    lines.append("")

    # 是否存在可行方案
    lines.append("## 结论\n")
    if summary.verdict == "viable":
        lines.append(f"选中方案：**{summary.selected_strategy}**")
        lines.append("")
        lines.append("⚠️ 本研究为历史模拟，不构成投资建议。过去表现不代表未来收益。")
    elif summary.verdict == "no_viable_strategy":
        lines.append("判定：`no_viable_strategy`。所有预注册方案在开发集和验证集上均未通过选择标准。")
    elif summary.verdict == "insufficient_evidence":
        lines.append("判定：`insufficient_evidence`。样本量不足以得出可靠结论。")
    else:
        lines.append(f"判定：`{summary.verdict}`")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Study orchestration
# ---------------------------------------------------------------------------


def _load_minute_coverage(
    coverage_path: Path,
    expected_freq: str,
    universe_membership: dict[str, tuple[str, str]] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Read and validate the persisted minute-health admission artifact."""
    if not coverage_path.exists():
        return None, "coverage report is missing"
    try:
        payload = json.loads(
            coverage_path.read_text("utf-8"),
            parse_constant=_reject_json_constant,
            parse_float=_parse_json_float,
        )
    except (OSError, json.JSONDecodeError, ValueError):
        return None, "coverage report is invalid JSON"
    if not isinstance(payload, dict):
        return None, "coverage report must be a JSON object"
    if payload.get("quality_version") != QUALITY_REPORT_VERSION:
        return payload, (
            "coverage report quality_version does not match the current "
            "minute quality gate"
        )
    if payload.get("source") != "tushare_stk_mins":
        return payload, "coverage report source is not tushare_stk_mins"
    if payload.get("freq") != expected_freq:
        return payload, (
            f"coverage frequency {payload.get('freq')!r} does not match "
            f"{expected_freq!r}"
        )
    if payload.get("passed") is not True:
        return payload, f"coverage passed=false: {payload.get('failures', [])}"
    failures = payload.get("failures")
    if not isinstance(failures, list) or failures:
        return payload, "coverage report has non-empty or invalid failures"
    try:
        coverage_pct = float(payload.get("coverage_pct", 0.0))
        symbols_checked = int(
            payload.get("symbols_checked", payload.get("symbols", 0))
        )
        total_bars = int(payload.get("total_bars", 0))
        total_rows = int(payload["total_rows"])
        zero_turnover_days = int(payload["zero_turnover_symbol_days"])
        excluded_zero_bars = int(payload["excluded_zero_turnover_bars"])
        unexpected_zero_days = int(
            payload["unexpected_zero_turnover_symbol_days"]
        )
    except KeyError:
        return payload, "coverage report is missing zero-turnover accounting"
    except (TypeError, ValueError):
        return payload, "coverage report has invalid numeric fields"
    if (
        not math.isfinite(coverage_pct)
        or coverage_pct < 95.0
        or coverage_pct > 100.0
    ):
        return payload, "coverage_pct is below 95% despite passed=true"
    if symbols_checked <= 0:
        return payload, "coverage report contains no checked symbols"
    if total_bars <= 0:
        return payload, "coverage report contains no minute bars"
    if (
        total_rows < total_bars
        or zero_turnover_days < 0
        or excluded_zero_bars < 0
        or unexpected_zero_days < 0
        or total_rows - total_bars != excluded_zero_bars
    ):
        return payload, "coverage report has inconsistent zero-turnover accounting"
    per_symbol = payload.get("per_symbol_coverage")
    if not isinstance(per_symbol, dict) or len(per_symbol) != symbols_checked:
        return payload, "coverage report has incomplete per-symbol coverage"
    try:
        normalized_per_symbol = {
            symbol: float(value) for symbol, value in per_symbol.items()
        }
    except (TypeError, ValueError):
        return payload, "coverage report has invalid per-symbol coverage"
    below_threshold = {
        symbol: value
        for symbol, value in normalized_per_symbol.items()
        if not math.isfinite(value) or value < 95.0 or value > 100.0
    }
    if below_threshold:
        return payload, "coverage report contains symbols below 95%"
    declared_expected = payload.get("expected_symbols")
    if (
        not isinstance(declared_expected, list)
        or not declared_expected
        or any(not isinstance(symbol, str) or not symbol for symbol in declared_expected)
        or len(declared_expected) != len(set(declared_expected))
    ):
        return payload, "coverage report has invalid expected_symbols"
    declared_set = set(declared_expected)
    if not declared_set.issubset(normalized_per_symbol):
        return payload, "coverage report omits expected symbols"
    for field_name in (
        "duplicate_keys",
        "non_monotonic_time",
        "invalid_ohlc",
        "invalid_numeric",
        "missing_required_value",
        "trade_date_mismatch",
        "non_trading_session",
        "missing_trading_days",
        "unexpected_zero_turnover_symbol_days",
    ):
        try:
            count = int(payload.get(field_name, 0))
        except (TypeError, ValueError):
            return payload, f"coverage report has invalid {field_name}"
        if count != 0:
            return payload, f"coverage report has nonzero {field_name}"
    try:
        missing_bars = int(payload.get("missing_bars", 0))
        symbols_with_data = int(payload.get("symbols_with_data", 0))
    except (TypeError, ValueError):
        return payload, "coverage report has invalid completeness fields"
    if missing_bars < 0:
        return payload, "coverage report has negative missing_bars"
    if not 0 < symbols_with_data <= symbols_checked:
        return payload, "coverage report has invalid symbols_with_data"
    symbol_ranges = payload.get("symbol_ranges")
    if not isinstance(symbol_ranges, dict) or len(symbol_ranges) != symbols_with_data:
        return payload, "coverage report has incomplete symbol_ranges"
    for symbol, date_range in symbol_ranges.items():
        if (
            symbol not in normalized_per_symbol
            or not isinstance(date_range, dict)
            or not date_range.get("first_time")
            or not date_range.get("last_time")
            or str(date_range["first_time"]) > str(date_range["last_time"])
        ):
            return payload, "coverage report has invalid symbol_ranges"
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return payload, "coverage report issues must be a list"
    if any(
        not isinstance(issue, dict) or issue.get("severity") == "error"
        for issue in issues
    ):
        return payload, "coverage report contains invalid or error issues"
    for field_name in ("start_date", "end_date", "generated_at"):
        if not payload.get(field_name):
            return payload, f"coverage report is missing {field_name}"
    try:
        datetime.strptime(str(payload["start_date"]), "%Y-%m-%d")
        datetime.strptime(str(payload["end_date"]), "%Y-%m-%d")
        generated_at = datetime.fromisoformat(
            str(payload["generated_at"]).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return payload, "coverage report contains invalid dates"
    if generated_at.tzinfo is None:
        return payload, "coverage generated_at must include a timezone"
    if str(payload["start_date"]) > str(payload["end_date"]):
        return payload, "coverage report date range is reversed"
    if universe_membership is not None:
        from .minute_data import _symbol_to_ts_code

        coverage_start = str(payload["start_date"]).replace("-", "")
        coverage_end = str(payload["end_date"]).replace("-", "")
        expected_symbols = {
            _symbol_to_ts_code(symbol)
            for symbol, (active_from, active_until) in (
                universe_membership.items()
            )
            if active_from <= coverage_end
            and active_until >= coverage_start
        }
        if not expected_symbols:
            return payload, (
                "research universe has no active members "
                "in the coverage interval"
            )
        if declared_set != expected_symbols:
            return payload, (
                "coverage report does not match the research universe"
            )
        if set(normalized_per_symbol) != expected_symbols:
            return payload, (
                "coverage report contains symbols outside "
                "the research universe"
            )

    payload = dict(payload)
    payload["coverage_report_path"] = str(coverage_path)
    payload["coverage_sha256"] = hashlib.sha256(coverage_path.read_bytes()).hexdigest()
    return payload, ""


def _coverage_date(value: Any, label: str) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"daily coverage has invalid {label}")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as error:
        raise ValueError(f"daily coverage has invalid {label}") from error
    return text


def _coverage_count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"daily coverage has invalid {label}")
    return value


def _load_daily_coverage(
    coverage_path: Path,
    expected_start: str,
    expected_end: str,
) -> tuple[dict[str, Any] | None, str]:
    """Validate the persisted point-in-time daily-data admission artifact."""
    try:
        payload = _read_json_object(coverage_path, "daily coverage report")
        if payload.get("source") != "tushare-pro-point-in-time":
            raise ValueError("daily coverage source is not point-in-time Tushare")
        if payload.get("passed") is not True:
            raise ValueError(
                f"daily coverage passed=false: {payload.get('failures', [])}"
            )
        failures = payload.get("failures")
        if not isinstance(failures, list) or failures:
            raise ValueError(
                "daily coverage has non-empty or invalid failures"
            )

        coverage_start = _coverage_date(
            payload.get("start_date"), "start_date"
        )
        coverage_end = _coverage_date(payload.get("end_date"), "end_date")
        required_start = _coverage_date(expected_start, "expected_start")
        required_end = _coverage_date(expected_end, "expected_end")
        if coverage_start > required_start or coverage_end < required_end:
            raise ValueError(
                "daily coverage does not span the requested study range"
            )

        trading_days = _coverage_count(
            payload.get("trading_days"), "trading_days"
        )
        common_days = _coverage_count(
            payload.get("common_required_days"),
            "common_required_days",
        )
        if trading_days <= 0 or common_days != trading_days:
            raise ValueError(
                "daily coverage does not contain complete required endpoints"
            )

        endpoint_days = payload.get("endpoint_days")
        required_endpoints = {
            "daily",
            "adj_factor",
            "daily_basic",
            "moneyflow",
            "stk_limit",
            "suspend_d",
        }
        if not isinstance(endpoint_days, dict):
            raise ValueError("daily coverage endpoint_days must be an object")
        for endpoint in required_endpoints:
            if (
                _coverage_count(
                    endpoint_days.get(endpoint),
                    f"endpoint_days.{endpoint}",
                )
                < trading_days
            ):
                raise ValueError(
                    f"daily coverage is incomplete for {endpoint}"
                )

        missing_days = payload.get("missing_required_days")
        if not isinstance(missing_days, dict) or any(
            not isinstance(value, list) or value
            for value in missing_days.values()
        ):
            raise ValueError(
                "daily coverage has missing required partitions"
            )

        references = payload.get("reference_tables")
        if not isinstance(references, dict) or any(
            references.get(name) is not True
            for name in ("stock_basic", "namechange", "trade_cal")
        ):
            raise ValueError(
                "daily coverage is missing required reference tables"
            )

        generated_at = datetime.fromisoformat(
            str(payload.get("generated_at") or "").replace("Z", "+00:00")
        )
        if generated_at.tzinfo is None:
            raise ValueError(
                "daily coverage generated_at must include a timezone"
            )
    except (TypeError, ValueError) as error:
        return None, str(error)

    result = dict(payload)
    result["coverage_report_path"] = str(coverage_path)
    result["coverage_sha256"] = _sha256_file(coverage_path)
    return result, ""


def _required_history_start(
    daily_data_root: Path,
    stage_start: str,
    minimum_listing_days: int,
) -> str:
    """Resolve enough authoritative sessions before a stage to build D features."""
    from .minute_data import load_trading_dates

    compact_start = stage_start.replace("-", "")
    history_dates = load_trading_dates(
        daily_data_root,
        "1900-01-01",
        compact_start,
    )
    prior_dates = [
        value for value in (history_dates or []) if value < compact_start
    ]
    required_prior_sessions = max(60, minimum_listing_days)
    if len(prior_dates) < required_prior_sessions:
        raise ValueError(
            "authoritative trade calendar has fewer than "
            f"{required_prior_sessions} sessions before {stage_start}"
        )
    return prior_dates[-required_prior_sessions]


def _with_daily_quality_failure(
    coverage: dict[str, Any],
    daily_coverage_path: Path,
    daily_coverage_hash: str,
    failure: str,
) -> dict[str, Any]:
    """Normalize a post-admission daily-data failure into quality.json."""
    result = dict(coverage)
    result["passed"] = False
    existing_daily = result.get("daily_coverage")
    daily = dict(existing_daily) if isinstance(existing_daily, dict) else {}
    daily["passed"] = False
    daily["coverage_report_path"] = str(daily_coverage_path)
    daily["coverage_sha256"] = daily_coverage_hash
    daily_failures = daily.get("failures")
    normalized_daily = (
        list(daily_failures) if isinstance(daily_failures, list) else []
    )
    if failure not in normalized_daily:
        normalized_daily.append(failure)
    daily["failures"] = normalized_daily
    result["daily_coverage"] = daily

    failures = result.get("failures")
    normalized_failures = list(failures) if isinstance(failures, list) else []
    if failure not in normalized_failures:
        normalized_failures.append(failure)
    result["failures"] = normalized_failures
    return result


def _load_development_returns(
    runtime_root: Path,
    config_hash: str,
    coverage_hash: str,
    daily_coverage_hash: str,
    universe_hash: str,
    expected_strategies: set[str],
) -> tuple[dict[str, float], str]:
    """Load the latest completed development result or fail closed."""
    latest_path = runtime_root / "latest.json"
    try:
        latest = _read_json_object(latest_path, "latest run pointer")
    except ValueError as error:
        return {}, str(error)
    run_id = latest.get("development")
    if not isinstance(run_id, str) or not run_id:
        return {}, "latest run pointer has no development run"
    run_dir = (runtime_root / run_id).resolve()
    runtime_resolved = runtime_root.resolve()
    if run_dir.parent != runtime_resolved:
        return {}, "development run pointer escapes runtime root"
    summary_path = run_dir / "summary.json"
    try:
        report = _read_json_object(summary_path, "development report")
    except ValueError as error:
        return {}, str(error)
    if report.get("stage") != "development":
        return {}, "development report has the wrong stage"
    if report.get("verdict") != "development_complete":
        return {}, "development report is not complete"
    if report.get("config_hash") != config_hash:
        return {}, "development report config hash mismatch"
    if report.get("coverage_sha256") != coverage_hash:
        return {}, "development report coverage hash mismatch"
    if report.get("daily_coverage_sha256") != daily_coverage_hash:
        return {}, "development report daily coverage hash mismatch"
    if report.get("universe_sha256") != universe_hash:
        return {}, "development report universe hash mismatch"
    strategies = report.get("strategies")
    if not isinstance(strategies, list):
        return {}, "development report strategies must be a list"
    mapped = _strategy_map(report)
    if len(mapped) != len(strategies):
        return {}, "development report contains duplicate strategy keys"
    missing = sorted(expected_strategies - set(mapped))
    if missing:
        return {}, f"development report missing strategies: {missing}"
    try:
        returns = {
            key: float(mapped[key]["mean_net_return"])
            for key in expected_strategies
        }
    except (KeyError, TypeError, ValueError):
        return {}, "development report has invalid mean returns"
    if any(not math.isfinite(value) for value in returns.values()):
        return {}, "development report has non-finite mean returns"
    return returns, ""


def run_rebound_study(
    config_path: Path | str,
    stage: str,
    runtime_root: Path | str,
    minute_root: Path | str,
    daily_data_root: Path | str,
    universe_path: Path | str,
    repo_root: Path | str,
    bootstrap_seed: int | None = None,
) -> StudySummary:
    """Run the rebound study for a given stage.

    Args:
        config_path: Path to rebound-v1.1.json config.
        stage: "development", "validation", or "frozen".
        runtime_root: research/runtime/rebound-v1.1/.
        minute_root: research/runtime/minute/.
        daily_data_root: research/runtime/data/.
        universe_path: web/data/universe.json.
        repo_root: Repository root for git state.
        bootstrap_seed: Random seed for bootstrap.

    Returns:
        StudySummary with results.
    """
    config = load_rebound_config(config_path)
    config_hash = hash_config(config)
    runtime_root = Path(runtime_root)
    minute_root = Path(minute_root)
    daily_data_root = Path(daily_data_root)
    repo_root = Path(repo_root)
    universe_path = Path(universe_path).resolve()
    universe_hash = _sha256_file(universe_path)
    if bootstrap_seed is None:
        bootstrap_seed = int(config.get("bootstrap_seed", 42))
    from .minute_data import (
        load_suspended_map,
        load_trading_dates,
    )

    universe_symbols = load_universe_membership(universe_path)

    # CRITICAL: Read ALL parameters from config, not module constants.
    data_window = config.get("data_window", {})
    dev_window = data_window.get("development", {})
    val_window = data_window.get("validation", {})
    frozen_window = data_window.get("frozen", {})
    cfg_dev_start = dev_window.get("start", DEV_START)
    cfg_dev_end = dev_window.get("end", DEV_END)
    cfg_val_start = val_window.get("start", VAL_START)
    cfg_val_end = val_window.get("end", VAL_END)
    cfg_frozen_start = frozen_window.get("start", FROZEN_START)

    selection_rules = config.get("selection_rules", {})
    cfg_min_val_trades = selection_rules.get("min_val_trades", 30)
    cfg_min_frozen_days = selection_rules.get("min_frozen_trading_days", MIN_FROZEN_TRADING_DAYS)
    cfg_min_frozen_events = selection_rules.get("min_frozen_events", MIN_FROZEN_EVENTS)

    # Config-driven strategies and hold periods
    cfg_strategies_list = config.get("entry_strategies", [])
    cfg_strategy_names = [s["name"] for s in cfg_strategies_list]
    cancel_conditions = config.get("cancel_conditions", {})
    gap_up_pct = float(cancel_conditions.get("gap_up_pct", 3.0))
    strategy_rules = {
        entry["name"]: EntryRule(
            gap_up_pct=gap_up_pct,
            min_confirm_time=str(entry.get("min_confirm_time", "09:45:00")),
            consecutive_bars=int(entry.get("consecutive_bars", 2)),
            latest_entry_time=str(entry.get("latest_entry_time", "14:30:00")),
        )
        for entry in cfg_strategies_list
    }
    cfg_hold_periods = config.get("hold_periods", HOLD_PERIODS)

    coverage_path = minute_root / "meta" / "coverage.json"
    daily_coverage_path = daily_data_root / "meta" / "coverage.json"
    coverage_data, coverage_error = _load_minute_coverage(
        coverage_path,
        str(config.get("freq", "5min")),
        universe_symbols,
    )
    if coverage_path.exists():
        coverage_hash = _sha256_file(coverage_path)
    else:
        coverage_hash = ""
    if coverage_data is not None:
        coverage_data = dict(coverage_data)
        coverage_data.setdefault("coverage_report_path", str(coverage_path))
        coverage_data.setdefault("coverage_sha256", coverage_hash)
    if coverage_error:
        coverage_data = dict(coverage_data or {})
        coverage_data["passed"] = False
        failures = coverage_data.get("failures")
        normalized_failures = list(failures) if isinstance(failures, list) else []
        if coverage_error not in normalized_failures:
            normalized_failures.append(coverage_error)
        coverage_data["failures"] = normalized_failures
        coverage_data.setdefault("coverage_report_path", str(coverage_path))
        coverage_data.setdefault("coverage_sha256", coverage_hash)

    locked_strategy = ""
    # Determine date range for this stage.
    if stage == "development":
        start, end = cfg_dev_start, cfg_dev_end
    elif stage == "validation":
        start, end = cfg_val_start, cfg_val_end
    elif stage == "frozen":
        completed_frozen = _find_completed_frozen_run(runtime_root)
        if completed_frozen:
            return StudySummary(
                stage=stage,
                config_hash=config_hash,
                run_at=datetime.now(UTC).isoformat(),
                coverage_sha256=coverage_hash,
                universe_sha256=universe_hash,
                verdict="blocked",
                note=(
                    "frozen acceptance already completed in "
                    f"{completed_frozen}; formal rerun refused"
                ),
            )
        # Verify config lock
        lock_path = runtime_root / "config-lock.json"
        verified, msg = verify_config_lock(config_path, lock_path)
        if not verified:
            summary = StudySummary(
                stage=stage,
                config_hash=config_hash,
                run_at=datetime.now(UTC).isoformat(),
                coverage_sha256=coverage_hash,
                universe_sha256=universe_hash,
                verdict="blocked",
                note=f"config lock verification failed: {msg}",
            )
            _write_outputs(
                runtime_root,
                stage,
                summary,
                config,
                repo_root,
                None,
                coverage_data,
            )
            return summary
        locked_strategy = str(
            _read_json_object(lock_path, "config lock")["selected_strategy"]
        )
        lock = _read_json_object(lock_path, "config lock")
        locked_minute_path = Path(lock["coverage_report_path"]).resolve()
        locked_daily_path = Path(lock["daily_coverage_report_path"]).resolve()
        locked_universe_path = Path(lock["universe_path"]).resolve()
        if (
            locked_minute_path != coverage_path.resolve()
            or locked_daily_path != daily_coverage_path.resolve()
            or locked_universe_path != universe_path
        ):
            summary = StudySummary(
                stage=stage,
                config_hash=config_hash,
                run_at=datetime.now(UTC).isoformat(),
                coverage_sha256=coverage_hash,
                universe_sha256=universe_hash,
                verdict="blocked",
                note=(
                    "config lock sources do not match the runtime data "
                    "and universe paths"
                ),
            )
            _write_outputs(
                runtime_root,
                stage,
                summary,
                config,
                repo_root,
                None,
                coverage_data,
            )
            return summary
        start = cfg_frozen_start
        end = str((coverage_data or {}).get("end_date") or "")
    else:
        raise ValueError(f"unknown stage: {stage}")

    if coverage_error:
        summary = StudySummary(
            stage=stage,
            config_hash=config_hash,
            run_at=datetime.now(UTC).isoformat(),
            coverage_sha256=coverage_hash,
            universe_sha256=universe_hash,
            verdict="blocked",
            note=f"minute quality gate failed: {coverage_error}",
        )
        _write_outputs(
            runtime_root, stage, summary, config, repo_root, None, coverage_data
        )
        return summary

    assert coverage_data is not None
    if not end:
        summary = StudySummary(
            stage=stage,
            config_hash=config_hash,
            run_at=datetime.now(UTC).isoformat(),
            coverage_sha256=coverage_hash,
            universe_sha256=universe_hash,
            verdict="blocked",
            note="minute quality gate failed: coverage end_date is missing",
        )
        _write_outputs(
            runtime_root, stage, summary, config, repo_root, None, coverage_data
        )
        return summary
    coverage_start = str(coverage_data["start_date"]).replace("-", "")
    coverage_end = str(coverage_data["end_date"]).replace("-", "")
    if coverage_start > start.replace("-", "") or coverage_end < end.replace("-", ""):
        summary = StudySummary(
            stage=stage,
            config_hash=config_hash,
            run_at=datetime.now(UTC).isoformat(),
            coverage_sha256=coverage_hash,
            universe_sha256=universe_hash,
            verdict="blocked",
            note=(
                "minute quality gate failed: coverage range "
                f"{coverage_data['start_date']}..{coverage_data['end_date']} "
                f"does not cover {start}..{end}"
            ),
        )
        _write_outputs(
            runtime_root, stage, summary, config, repo_root, None, coverage_data
        )
        return summary

    daily_coverage_hash = (
        _sha256_file(daily_coverage_path)
        if daily_coverage_path.exists()
        else ""
    )
    coverage_data = dict(coverage_data)
    start_compact = start.replace("-", "")
    end_compact = end.replace("-", "")
    try:
        lookback_start = _required_history_start(
            daily_data_root,
            start,
            int(config["event_thresholds"]["min_listing_days"]),
        )
    except ValueError as error:
        daily_history_error = str(error)
        coverage_data = _with_daily_quality_failure(
            coverage_data,
            daily_coverage_path,
            daily_coverage_hash,
            daily_history_error,
        )
        summary = StudySummary(
            stage=stage,
            config_hash=config_hash,
            run_at=datetime.now(UTC).isoformat(),
            coverage_sha256=coverage_hash,
            daily_coverage_sha256=daily_coverage_hash,
            universe_sha256=universe_hash,
            verdict="blocked_by_daily_data",
            note=f"daily data quality gate failed: {daily_history_error}",
        )
        _write_outputs(
            runtime_root,
            stage,
            summary,
            config,
            repo_root,
            None,
            coverage_data,
        )
        return summary

    trading_dates = load_trading_dates(daily_data_root, start, end)
    if not trading_dates:
        calendar_error = (
            "authoritative trade calendar is missing for the study range"
        )
        coverage_data = _with_daily_quality_failure(
            coverage_data,
            daily_coverage_path,
            daily_coverage_hash,
            calendar_error,
        )
        summary = StudySummary(
            stage=stage,
            config_hash=config_hash,
            run_at=datetime.now(UTC).isoformat(),
            coverage_sha256=coverage_hash,
            daily_coverage_sha256=daily_coverage_hash,
            universe_sha256=universe_hash,
            verdict="blocked_by_daily_data",
            note=calendar_error,
        )
        _write_outputs(
            runtime_root,
            stage,
            summary,
            config,
            repo_root,
            None,
            coverage_data,
        )
        return summary

    daily_coverage, daily_coverage_error = _load_daily_coverage(
        daily_coverage_path,
        lookback_start,
        end,
    )
    if daily_coverage_error:
        coverage_data = _with_daily_quality_failure(
            coverage_data,
            daily_coverage_path,
            daily_coverage_hash,
            daily_coverage_error,
        )
        summary = StudySummary(
            stage=stage,
            config_hash=config_hash,
            run_at=datetime.now(UTC).isoformat(),
            coverage_sha256=coverage_hash,
            daily_coverage_sha256=daily_coverage_hash,
            universe_sha256=universe_hash,
            verdict="blocked_by_daily_data",
            note=f"daily data quality gate failed: {daily_coverage_error}",
        )
        _write_outputs(
            runtime_root,
            stage,
            summary,
            config,
            repo_root,
            None,
            coverage_data,
        )
        return summary
    assert daily_coverage is not None
    daily_coverage_hash = str(daily_coverage["coverage_sha256"])
    coverage_data["daily_coverage"] = daily_coverage

    expected_strategy_keys = {
        f"{strategy_name}_{hold_days}d"
        for strategy_name in cfg_strategy_names
        for hold_days in cfg_hold_periods
    }
    development_returns: dict[str, float] = {}
    if stage == "validation":
        development_returns, development_error = _load_development_returns(
            runtime_root,
            config_hash,
            coverage_hash,
            daily_coverage_hash,
            universe_hash,
            expected_strategy_keys,
        )
        if development_error:
            summary = StudySummary(
                stage=stage,
                config_hash=config_hash,
                run_at=datetime.now(UTC).isoformat(),
                coverage_sha256=coverage_hash,
                daily_coverage_sha256=daily_coverage_hash,
                universe_sha256=universe_hash,
                verdict="blocked",
                note=f"development evidence invalid: {development_error}",
            )
            _write_outputs(
                runtime_root,
                stage,
                summary,
                config,
                repo_root,
                None,
                coverage_data,
            )
            return summary

    suspended_map = load_suspended_map(daily_data_root, start, end)

    # Load daily data
    daily_df = _load_daily_data(daily_data_root, lookback_start, end_compact)
    if daily_df is None or daily_df.height == 0:
        daily_data_error = "daily data not available for the study period"
        coverage_data = _with_daily_quality_failure(
            coverage_data,
            daily_coverage_path,
            daily_coverage_hash,
            daily_data_error,
        )
        summary = StudySummary(
            stage=stage,
            config_hash=config_hash,
            run_at=datetime.now(UTC).isoformat(),
            coverage_sha256=coverage_hash,
            daily_coverage_sha256=daily_coverage_hash,
            universe_sha256=universe_hash,
            verdict="blocked_by_daily_data",
            note=daily_data_error,
        )
        _write_outputs(
            runtime_root, stage, summary, config, repo_root, None, coverage_data
        )
        return summary

    # CRITICAL: Read event thresholds from config.
    event_thresholds = config.get("event_thresholds", {})
    # Config min_20d_avg_amount is in yuan; Tushare amount is in 千元.
    cfg_min_amount_yuan = event_thresholds.get("min_20d_avg_amount", 50_000_000.0)
    cfg_min_amount = cfg_min_amount_yuan / 1000.0  # Convert yuan → 千元

    events = detect_events(
        daily_df, universe_symbols, start_compact, end_compact,
        min_listing_days=event_thresholds.get("min_listing_days", 60),
        max_60d_position_pct=event_thresholds.get("max_60d_position_pct", 25.0),
        min_60d_drawdown_pct=event_thresholds.get("min_60d_drawdown_pct", 20.0),
        max_5d_return_pct=event_thresholds.get("max_5d_return_pct", -6.0),
        min_20d_avg_amount=cfg_min_amount,
    )

    # Check minimum evidence (frozen only)
    if stage == "frozen":
        # Count authoritative market-open dates, not whichever daily rows
        # happened to survive loading.
        trading_days = len(trading_dates)
        if trading_days < cfg_min_frozen_days or len(events) < cfg_min_frozen_events:
            summary = StudySummary(
                stage=stage,
                config_hash=config_hash,
                run_at=datetime.now(UTC).isoformat(),
                coverage_sha256=coverage_hash,
                daily_coverage_sha256=daily_coverage_hash,
                universe_sha256=universe_hash,
                verdict="insufficient_evidence",
                note=f"trading_days={trading_days}, events={len(events)}; "
                     f"need >={cfg_min_frozen_days} days and >={cfg_min_frozen_events} events",
            )
            _write_outputs(
                runtime_root, stage, summary, config, repo_root, None, coverage_data
            )
            return summary

    # Run strategies - read config from nested execution/portfolio sections
    exec_section = config.get("execution", {})
    portfolio_section = config.get("portfolio", {})
    exit_rules = config.get("exit_rules", {})

    exec_config = ExecutionConfig(
        fee_bps=exec_section.get("fee_bps", 10.0),
        slippage_bps=exec_section.get("slippage_bps", 5.0),
        capacity_pct=exec_section.get("capacity_pct", 1.0),
        lot_size=exec_section.get("lot_size", 100),
    )
    stop_loss_pct = exit_rules.get("stop_loss_pct", 5.0) / 100.0
    capital = portfolio_section.get("capital_per_trade", 10000.0)

    all_strategies: list[StrategyResult] = []

    strategy_pairs = [
        (strategy_name, hold_days)
        for strategy_name in cfg_strategy_names
        for hold_days in cfg_hold_periods
    ]
    if stage == "frozen":
        strategy_pairs = [
            pair
            for pair in strategy_pairs
            if f"{pair[0]}_{pair[1]}d" == locked_strategy
        ]
        if len(strategy_pairs) != 1:
            raise ValueError(
                "locked strategy does not resolve to one pre-registered pair"
            )

    # Use all pre-registered pairs for development/validation, but expose only
    # the immutable selected pair to the frozen acceptance sample.
    for strategy_name, hold_days in strategy_pairs:
        detect_fn = ENTRY_STRATEGIES.get(strategy_name)
        if detect_fn is None:
            continue
        trades = _run_strategy_on_events(
            events=events,
            strategy_name=strategy_name,
            detect_fn=detect_fn,
            hold_days=hold_days,
            minute_root=minute_root,
            daily_df=daily_df,
            exec_config=exec_config,
            stop_loss_pct=stop_loss_pct,
            capital=capital,
            trading_dates=trading_dates,
            suspended_map=suspended_map,
            entry_rule=strategy_rules[strategy_name],
        )
        stats = compute_strategy_stats(
            strategy_name,
            hold_days,
            trades,
            bootstrap_seed,
            portfolio_capital=portfolio_section.get(
                "portfolio_capital", PORTFOLIO_CAPITAL
            ),
            risk_budget_pct=portfolio_section.get(
                "risk_budget_pct", RISK_BUDGET_PCT
            ),
            position_cap_pct=portfolio_section.get(
                "position_cap_pct", POSITION_CAP_PCT
            ),
            stop_loss_pct_cfg=exit_rules.get(
                "stop_loss_pct", STOP_LOSS_PCT
            ),
        )
        all_strategies.append(stats)

    # Select strategy (only for validation stage)
    selected = ""
    verdict = "viable"
    note = ""

    if stage == "validation":
        # Filter strategies: both dev and val must have positive mean_net_return
        viable_strategies = []
        for s in all_strategies:
            key = f"{s.strategy_name}_{s.hold_days}d"
            dev_ret = development_returns[key]
            if dev_ret > 0 and s.mean_net_return > 0:
                viable_strategies.append(s)

        if not viable_strategies:
            verdict = "no_viable_strategy"
            note = "no strategy has both dev and val mean_net_return > 0"
        else:
            selected, verdict = select_strategy(viable_strategies, min_trades=cfg_min_val_trades)
            if verdict == "no_viable_strategy":
                note = f"no strategy passed validation with >= {cfg_min_val_trades} trades"
    elif stage == "development":
        verdict = "development_complete"
        note = f"{len(events)} events detected, {len(all_strategies)} strategy combinations evaluated"
    elif stage == "frozen":
        # Frozen just reports, doesn't select
        selected = locked_strategy
        verdict = "frozen_complete"
        note = (
            f"frozen acceptance for locked strategy {locked_strategy}; "
            "results cannot be used to modify thresholds"
        )

    summary = StudySummary(
        stage=stage,
        config_hash=config_hash,
        run_at=datetime.now(UTC).isoformat(),
        coverage_sha256=coverage_hash,
        daily_coverage_sha256=daily_coverage_hash,
        universe_sha256=universe_hash,
        strategies=all_strategies,
        selected_strategy=selected,
        verdict=verdict,
        note=note,
    )

    _write_outputs(
        runtime_root, stage, summary, config, repo_root, events, coverage_data
    )
    return summary


def _load_endpoint_data(
    data_root: Path,
    endpoint: str,
    start: str,
    end: str,
) -> pl.DataFrame | None:
    raw_root = data_root / "raw" / endpoint
    if not raw_root.exists():
        return None
    frames: list[pl.DataFrame] = []
    for parquet_file in sorted(raw_root.glob("trade_date=*/part.parquet")):
        trade_date = parquet_file.parent.name.removeprefix("trade_date=")
        if trade_date < start or trade_date > end:
            continue
        if parquet_file.stat().st_size > 0:
            frames.append(pl.read_parquet(parquet_file))
    if not frames:
        return None
    return pl.concat(frames, how="diagonal_relaxed").with_columns(
        pl.col("trade_date").cast(pl.String),
        pl.col("ts_code").cast(pl.String),
    )


def _find_completed_frozen_run(runtime_root: Path) -> str:
    """Return the prior formal frozen run id, if one already exists."""
    for summary_path in sorted(
        runtime_root.glob("frozen-*/summary.json")
    ):
        try:
            payload = _read_json_object(
                summary_path, "prior frozen summary"
            )
        except ValueError:
            continue
        if payload.get("verdict") in {
            "frozen_complete",
            "insufficient_evidence",
        }:
            return summary_path.parent.name
    return ""


def _load_daily_data(data_root: Path, start: str, end: str) -> pl.DataFrame | None:
    """Load daily bars with exact adjustment, price-limit, ST, and suspension data."""
    daily = _load_endpoint_data(data_root, "daily", start, end)
    if daily is None:
        return None
    required_daily_columns = {
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "amount",
    }
    if not required_daily_columns.issubset(daily.columns):
        return None
    daily = daily.unique(
        subset=["ts_code", "trade_date"], keep="last"
    ).sort(["ts_code", "trade_date"])

    for endpoint, columns in (
        ("adj_factor", ["adj_factor"]),
        ("stk_limit", ["up_limit", "down_limit"]),
    ):
        auxiliary = _load_endpoint_data(data_root, endpoint, start, end)
        if auxiliary is None or not all(column in auxiliary.columns for column in columns):
            for column in columns:
                if column not in daily.columns:
                    daily = daily.with_columns(pl.lit(None).alias(column))
            continue
        auxiliary = (
            auxiliary.select(["ts_code", "trade_date", *columns])
            .unique(subset=["ts_code", "trade_date"], keep="last")
        )
        for column in columns:
            if column in daily.columns:
                daily = daily.drop(column)
        daily = daily.join(auxiliary, on=["ts_code", "trade_date"], how="left")

    suspension = _load_endpoint_data(data_root, "suspend_d", start, end)
    if suspension is None or not {"ts_code", "trade_date"}.issubset(
        suspension.columns
    ):
        if "suspended" not in daily.columns:
            endpoint_root = data_root / "raw" / "suspend_d"
            default_value: bool | None = False if endpoint_root.exists() else None
            daily = daily.with_columns(
                pl.lit(default_value).cast(pl.Boolean).alias("suspended")
            )
    else:
        suspended_keys = suspension.select("ts_code", "trade_date").unique().with_columns(
            pl.lit(True).alias("suspended")
        )
        if "suspended" in daily.columns:
            daily = daily.drop("suspended")
        daily = (
            daily.join(suspended_keys, on=["ts_code", "trade_date"], how="left")
            .with_columns(pl.col("suspended").fill_null(False))
        )

    namechange_path = data_root / "reference" / "namechange.parquet"
    if namechange_path.exists():
        try:
            changes = pl.read_parquet(namechange_path)
        except Exception:
            changes = pl.DataFrame()
        required_namechange = {"ts_code", "name", "start_date", "end_date"}
        if changes.height == 0 or not required_namechange.issubset(
            changes.columns
        ):
            if "is_st" not in daily.columns:
                daily = daily.with_columns(
                    pl.lit(None).cast(pl.Boolean).alias("is_st")
                )
        else:
            st_ranges: dict[str, list[tuple[str, str]]] = {}
            for row in changes.iter_rows(named=True):
                name = str(row.get("name") or "").upper()
                if "ST" not in name:
                    continue
                ts_code = str(row.get("ts_code") or "")
                start_date = _compact_reference_date(
                    row.get("start_date"), "00000000"
                )
                end_date = _compact_reference_date(
                    row.get("end_date"), "99999999"
                )
                st_ranges.setdefault(ts_code, []).append(
                    (start_date, end_date)
                )
            is_st = [
                any(
                    left <= trade_date <= right
                    for left, right in st_ranges.get(ts_code, [])
                )
                for ts_code, trade_date in daily.select(
                    "ts_code", "trade_date"
                ).iter_rows()
            ]
            if "is_st" in daily.columns:
                daily = daily.drop("is_st")
            daily = daily.with_columns(
                pl.Series("is_st", is_st, dtype=pl.Boolean)
            )
    elif "is_st" not in daily.columns:
        daily = daily.with_columns(pl.lit(None).cast(pl.Boolean).alias("is_st"))

    if "adj_factor" not in daily.columns:
        daily = daily.with_columns(pl.lit(None).alias("adj_factor"))
    for column in ("up_limit", "down_limit"):
        if column not in daily.columns:
            daily = daily.with_columns(pl.lit(None).alias(column))

    return daily


def _compact_reference_date(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip().replace("-", "")
    if text.endswith(".0"):
        text = text[:-2]
    return text if len(text) == 8 and text.isdigit() else default


def _previous_close_on_next_day_basis(
    event: ReboundEvent,
    d1_row: dict[str, Any],
) -> float | None:
    """Convert D raw close to the equivalent D+1 raw-price basis."""
    d_factor = event.adj_factor_d
    d1_factor = d1_row.get("adj_factor")
    values = (event.close_d, d_factor, d1_factor)
    if any(value is None for value in values):
        return None
    close_d, factor_d, factor_d1 = (float(value) for value in values)
    if not all(
        math.isfinite(value) and value > 0
        for value in (close_d, factor_d, factor_d1)
    ):
        return None
    return close_d * factor_d / factor_d1


def _run_strategy_on_events(
    events: list[ReboundEvent],
    strategy_name: str,
    detect_fn: Any,
    hold_days: int,
    minute_root: Path,
    daily_df: pl.DataFrame,
    exec_config: ExecutionConfig,
    stop_loss_pct: float,
    capital: float,
    trading_dates: list[str],
    suspended_map: dict[str, set[str]],
    entry_rule: EntryRule,
) -> list[TradeResult]:
    """Run a single strategy across all events."""
    from .minute_data import load_minute_bars

    trades: list[TradeResult] = []

    for event in events:
        ts_code = event.ts_code
        decision_date = event.decision_date

        market_dates = [value for value in trading_dates if value > decision_date]
        if not market_dates:
            trades.append(TradeResult(
                event_id=event.event_id,
                symbol=event.symbol,
                decision_date=decision_date,
                no_fill_reason="no_fill_no_next_day",
            ))
            continue

        d1_date = market_dates[0]
        suspended_dates = suspended_map.get(ts_code, set())
        if d1_date in suspended_dates:
            trades.append(TradeResult(
                event_id=event.event_id,
                symbol=event.symbol,
                decision_date=decision_date,
                no_fill_reason="no_fill_suspended",
            ))
            continue

        sym_daily = daily_df.filter(pl.col("ts_code") == ts_code).sort("trade_date")
        d1_daily = sym_daily.filter(pl.col("trade_date") == d1_date)
        if d1_daily.height == 0:
            trades.append(TradeResult(
                event_id=event.event_id,
                symbol=event.symbol,
                decision_date=decision_date,
                no_fill_reason="no_fill_missing_daily_data",
            ))
            continue
        d1_row = d1_daily.row(0, named=True)
        entry_up_limit = d1_row.get("up_limit")
        prev_close_d1 = _previous_close_on_next_day_basis(event, d1_row)
        if prev_close_d1 is None:
            trades.append(TradeResult(
                event_id=event.event_id,
                symbol=event.symbol,
                decision_date=decision_date,
                no_fill_reason="no_fill_missing_adj_factor",
            ))
            continue

        # Load D+1 minute bars
        bars_d1 = load_minute_bars(minute_root, ts_code, d1_date, d1_date, "5min")
        if bars_d1.height == 0:
            trades.append(TradeResult(
                event_id=event.event_id,
                symbol=event.symbol,
                decision_date=decision_date,
                no_fill_reason="no_fill_data_missing",
            ))
            continue

        # Detect entry signal
        signal_idx = detect_fn(bars_d1, prev_close_d1, ts_code, entry_rule)
        if signal_idx is None:
            trades.append(TradeResult(
                event_id=event.event_id,
                symbol=event.symbol,
                decision_date=decision_date,
                no_fill_reason="no_fill_no_signal",
            ))
            continue

        # Market calendar, not symbol rows, defines D+1 and the legal exit day.
        if len(market_dates) <= hold_days:
            trades.append(TradeResult(
                event_id=event.event_id,
                symbol=event.symbol,
                decision_date=decision_date,
                no_fill_reason="no_fill_no_exit_data",
            ))
            continue

        eligible_exit_date = market_dates[hold_days]
        # Keep every remaining market date in scope so a suspended,
        # limit-down, or capacity-blocked exit can remain pending until the
        # first genuinely tradable bar. A fixed four-day buffer biases results.
        exit_market_dates = market_dates
        exit_start = d1_date
        exit_end = exit_market_dates[-1]
        bars_exit = load_minute_bars(minute_root, ts_code, exit_start, exit_end, "5min")

        down_limits: dict[str, float] = {}
        for row in sym_daily.filter(
            pl.col("trade_date").is_in(exit_market_dates)
        ).iter_rows(named=True):
            value = row.get("down_limit")
            if value is not None and float(value) > 0:
                down_limits[str(row["trade_date"])] = float(value)

        # CRITICAL: Build adj_factors map for cross-ex-date return calculation.
        adj_factors_map: dict[str, float] = {}
        sym_all = daily_df.filter(pl.col("ts_code") == ts_code)
        for arow in sym_all.iter_rows(named=True):
            td_a = str(arow["trade_date"])
            adj_val = arow.get("adj_factor")
            if adj_val is not None and float(adj_val) > 0:
                adj_factors_map[td_a] = float(adj_val)

        trade = simulate_trade(
            event_id=event.event_id,
            symbol=event.symbol,
            ts_code=ts_code,
            decision_date=decision_date,
            entry_bars=bars_d1,
            exit_bars=bars_exit,
            entry_signal_idx=signal_idx,
            hold_days=hold_days,
            stop_loss_pct=stop_loss_pct,
            config=exec_config,
            capital_per_trade=capital,
            entry_up_limit=float(entry_up_limit) if entry_up_limit is not None else None,
            exit_down_limits=down_limits,
            suspended_dates=suspended_dates,
            adj_factors=adj_factors_map,
            eligible_exit_date=eligible_exit_date,
        )

        trades.append(trade)

    return trades


# ---------------------------------------------------------------------------
# Output writing
# ---------------------------------------------------------------------------


def _write_outputs(
    runtime_root: Path,
    stage: str,
    summary: StudySummary,
    config: dict[str, Any],
    repo_root: Path,
    events: list[ReboundEvent] | None,
    coverage: dict[str, Any] | None = None,
) -> None:
    """Write all study outputs to a unique run-id directory.

    Layout per development plan §M5:
    - Full artifacts go to runtime_root/<run-id>/ (timestamp, preserves history)
    - A stable pointer runtime_root/latest.json maps stage → latest run-id
      for config-lock verification and cross-stage references.
    """
    run_id = f"{stage}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}"
    run_dir = runtime_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Manifest
    manifest = create_manifest(
        repo_root,
        config,
        stage,
        str((coverage or {}).get("end_date") or summary.run_at[:10]),
        coverage=coverage,
    )
    manifest["run_id"] = run_id
    manifest["universe_sha256"] = summary.universe_sha256
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )

    # Summary
    summary_json = (
        json.dumps(
            summary.to_dict(),
            ensure_ascii=False,
            indent=2,
            default=str,
            allow_nan=False,
        )
        + "\n"
    )
    (run_dir / "summary.json").write_text(summary_json, "utf-8")

    # Events parquet. Write the artifact even when the admitted event set is
    # empty so every run has the pre-registered output layout.
    event_rows = [
            {
                "event_id": e.event_id,
                "symbol": e.symbol,
                "ts_code": e.ts_code,
                "decision_date": e.decision_date,
                "close_d": e.close_d,
                "position_60d_pct": e.position_60d_pct,
                "drawdown_60d_pct": e.drawdown_60d_pct,
                "return_5d_pct": e.return_5d_pct,
                "avg_amount_20d": e.avg_amount_20d,
                "adj_factor_d": e.adj_factor_d,
            }
            for e in (events or [])
        ]
    events_df = pl.DataFrame(
        event_rows,
        schema={
            "event_id": pl.String,
            "symbol": pl.String,
            "ts_code": pl.String,
            "decision_date": pl.String,
            "close_d": pl.Float64,
            "position_60d_pct": pl.Float64,
            "drawdown_60d_pct": pl.Float64,
            "return_5d_pct": pl.Float64,
            "avg_amount_20d": pl.Float64,
            "adj_factor_d": pl.Float64,
        },
    )
    events_df.write_parquet(run_dir / "events.parquet", compression="zstd")

    # Trades parquet (flatten all strategy trades)
    all_trades: list[dict[str, Any]] = []
    for s in summary.strategies:
        for t in s.trades:
            t_copy = dict(t)
            t_copy["strategy"] = s.strategy_name
            t_copy["hold_days"] = s.hold_days
            all_trades.append(t_copy)
    trade_schema = {
        "event_id": pl.String,
        "symbol": pl.String,
        "decision_date": pl.String,
        "entry_signal_time": pl.String,
        "entry_time": pl.String,
        "entry_price_raw": pl.Float64,
        "entry_price_with_cost": pl.Float64,
        "shares": pl.Int64,
        "entry_reason": pl.String,
        "exit_signal_time": pl.String,
        "exit_time": pl.String,
        "exit_price_raw": pl.Float64,
        "exit_price_with_cost": pl.Float64,
        "exit_reason": pl.String,
        "gross_return": pl.Float64,
        "net_return": pl.Float64,
        "pnl_per_10000": pl.Float64,
        "mfe": pl.Float64,
        "mae": pl.Float64,
        "t1_blocked_stop": pl.Boolean,
        "pending_exit_bars": pl.Int64,
        "no_fill_reason": pl.String,
        "strategy": pl.String,
        "hold_days": pl.Int64,
    }
    trades_df = pl.DataFrame(all_trades, schema=trade_schema)
    trades_df.write_parquet(run_dir / "trades.parquet", compression="zstd")

    # Report
    report_md = generate_report_md(summary, stage, config)
    (run_dir / "report.md").write_text(report_md, "utf-8")

    quality_result = coverage or {
        "passed": False,
        "note": "no validated minute coverage report supplied",
    }
    (run_dir / "quality.json").write_text(
        json.dumps(
            quality_result,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        "utf-8",
    )

    # CRITICAL: Update latest.json pointer (stage → run_id).
    # This is the ONLY stable reference for cross-stage reads.
    latest_path = runtime_root / "latest.json"
    latest: dict[str, str] = {}
    if latest_path.exists():
        try:
            latest = json.loads(latest_path.read_text("utf-8"))
        except Exception:
            latest = {}
    latest[stage] = run_id
    latest_path.write_text(
        json.dumps(latest, ensure_ascii=False, indent=2, allow_nan=False)
        + "\n",
        "utf-8",
    )
