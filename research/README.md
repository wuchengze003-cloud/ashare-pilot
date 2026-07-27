# A-share research runtime

This Python 3.11 environment is isolated from `pyserver`. It owns historical
research data, Qlib experiments, the append-only decision ledger and model
promotion state. It never rewrites the V1 strategy curve.

```bash
uv sync --group dev
uv run ashare-research health
uv run ashare-research ledger-init
uv run pytest
```

Runtime data, models, MLflow runs and registries live under `runtime/` and are
ignored by Git. A model cannot become active unless every deterministic
promotion gate passes.

The daily Web pipeline may run registered model inference, but never calls
`train`, Optuna or promotion. Champion and challenger predictions are emitted
to separate files; only the champion adapter can produce executable orders.

Historical bootstrap:

```bash
uv run ashare-research bootstrap-qlib
uv run ashare-research qlib-data-health
uv run ashare-research qlib-benchmark --model-type linear
uv run ashare-research data-sync --start 2018-01-01 --end 2026-07-08
uv run ashare-research run-challenger --model-type linear --optuna-trials 0
uv run ashare-research run-challenger --model-type lightgbm --optuna-trials 20
```

The community Qlib snapshot is cold-start evidence only. Its benchmark result
is displayed in Dashboard but cannot enter the model registry. Promotion
requires the separate Tushare point-in-time dataset to pass quality checks.

The promotable feature contract is `ashare-core-v3`: transparent price/volume
features plus A-share turnover, money-flow, market breadth and regime features.
It predicts fee-adjusted cross-sectional excess returns and downside returns,
and excludes point-in-time ST and first-60-bar samples. Official Qlib
Alpha158 is kept as a separate cold-start benchmark so it can challenge this
baseline without being confused with production Tushare features.

The V2 evidence chain and current blockers are documented in
[`V2_DESIGN.md`](./V2_DESIGN.md).

Promotion is separate from training. Candidate metrics must be generated from
purged out-of-sample and shadow results, then checked with
`evaluate-promotion`. A same-date health file can trigger deterministic
rollback through `monitor`; if there is no previous ML champion the registry
falls back to V1.

Promotion evidence binds every source report and the champion metrics to a
path and SHA-256 digest. The registry verifies the files and recomputes the
candidate metrics before applying the gates; edited metric JSON cannot be used
to make promotion easier.

`drift` compares the latest 20 trading days with the preceding 120 trading
days using Evidently PSI. Explicit reference/current date windows are also
supported. Missing quality or drift evidence fails closed during inference.

`run-challenger` is the bounded experiment entry point. It rebuilds features,
runs quality and Evidently checks, trains the model, evaluates purged OOS and
the frozen holdout, writes native LightGBM TreeSHAP/linear contributions, and
registers an immutable shadow candidate. It never promotes directly.

The deterministic close pipeline fills matured 1/3/5/10-day outcomes before
new inference, assesses every promotion gate, and activates a passing champion
on the next decision day. Fewer than four qualifying stocks leave cash rather
than concentrating the portfolio.

`--fee-bps` is a one-way execution cost. Labels and matured outcomes deduct
twice that value for an entry-and-exit round trip; portfolio executions charge
the value on each actual trade.

The data and execution patterns are adapted from the MIT-licensed
`tickflow-stock-panel`; Qlib, LightGBM, Optuna and Evidently remain upstream
dependencies rather than copied implementations.

## V1.1 低位反弹事件研究

V1.1 是一个隔离的分钟级事件研究工具，回答"低位博反弹时，哪种入场确认
能提高净期望收益"。它不改变 V1 信号、不接入真实持仓、不生成买卖建议。

```bash
# 分钟数据探测
uv run ashare-research minute-probe --symbols 000001,300308,688256 --date 2025-07-01 --freq 5min

# 分钟数据同步
uv run ashare-research minute-sync --start 2025-01-01 --end 2026-07-23 --freq 5min --universe ../web/data/universe.json

# 分钟数据健康检查
uv run ashare-research minute-health --start 2025-01-01 --end 2026-07-23 --freq 5min

# 低位反弹事件研究
uv run ashare-research rebound-study --stage development --config config/rebound-v1.1.json
uv run ashare-research rebound-study --stage validation --config config/rebound-v1.1.json
uv run ashare-research rebound-lock --config config/rebound-v1.1.json
uv run ashare-research rebound-study --stage frozen --config config/rebound-v1.1.json
```

V1.1 研究产物位于 `runtime/rebound-v1.1/`，包括事件 Parquet、交易明细、
统计汇总和中文报告。配置锁定后生成 `config-lock.json`，冻结验收必须校验
配置哈希一致。

V1.1 与 V2 的边界：V1.1 只改善执行研究（D+1 分时入场条件），不改变 D 收盘
信号的时间边界，不产生可晋级模型，不进入 V2 的 promotion 流程。
