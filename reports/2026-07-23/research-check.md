# Research 目录检查报告 · 策略进化状态摸底

- 检查人：Research_检查员_A3（只读检查，未训练/调参/晋级/改注册表）
- 检查时间：2026-07-23（本地）
- 范围：`research/` 目录、`web/data/runtime/ml/status.json`（只读引用）

---

## 1. 测试与环境（pytest / health）

| 项目 | 结果 |
|---|---|
| `uv sync --group dev` | 通过，242 个包审计无变更（Python 3.11.15） |
| `uv run pytest -q` | **54 passed**, 0 failed，耗时 4.46s（21 个测试文件） |
| `uv run ashare-research health` | 通过：qlib / lightgbm / optuna / evidently / polars / duckdb / pyarrow / mlflow 全部可用；`ledger: null`；registry 为空 |

警告（非阻断，技术债）：evidently 触发 `numpy.core` DeprecationWarning；litestar 2.23.0 弃用告警 6 条。

## 2. 账本完整性结论

- **`runtime/ledger.db` 不存在**（`runtime/registry/` 目录同样不存在）。账本从未初始化，predictions / decisions / executions / outcomes 四表均为零记录。
- 关于"hash 链"：经代码核查（`ledger.py` + 全包 grep），**本账本设计上没有逐行 hash 链**。其 append-only 完整性依赖：① SQLite 复合主键 + `INSERT OR IGNORE` 幂等写入（重复主键静默拒绝）；② WAL 模式；③ 模型 manifest / promotion evidence 上的 SHA-256 不可变哈希（`registry.py::_verify_manifest`）。
- 结论：账本缺失，完整性" vacuously 成立"（无数据可篡改）；但需说明当前实现与"hash 链"预期不同——完整性机制是主键幂等 + 工件哈希，不是链式哈希。这是设计事实，不是缺陷，但文档表述应对齐。

## 3. 当前进化状态

### 3.1 Champion / Challenger

- **Champion（ML）：无**。注册表为空（active=null, candidates=[], history=[], retired=[]），`active_model.json` 不存在。
- **生产策略：V1 规则策略**（在 ML 注册表之外）。`web/data/runtime/ml/status.json`（今日 07:19 UTC 生成）确认 `status: "v1-only"`，`active_model: null`，`challenger_models: []`，`promotion_assessments: []`。
- **Challenger：无任何注册候选模型**。

### 3.2 V1 基线（今日刷新，新鲜）

`runtime/baselines/v1/metrics.json`（2026-07-23 15:19 本地刷新，data_cutoff=2026-07-23）：

| 指标 | 数值 |
|---|---|
| Sharpe | 1.824 |
| 最大回撤 | **-29.09%**（V2_DESIGN.md 中 7-16 快照为 26.48%，已恶化） |
| 换手率 | 3784.5%（设计文档快照 3550.69%） |
| 区间收益 | +40.96%（2026-02-24 → 2026-07-23，103 个交易日） |
| 已平仓交易 | 71 笔；平均持有 6.19 根 K 线 |
| OOS 折数 | **0**（警告：V1 has no pre-2026 multi-fold OOS portfolio record） |

### 3.3 最近一次 challenger 实验（社区数据，不可晋级）

`runtime/benchmarks/` 下两次 Alpha158 冷启动基准（数据源：chenditc/investment_data release 2026-07-14，均标记 `promotable: false`）：

| 实验 | 日期 | OOS 折数 | 中位 RankIC | 冻结 holdout RankIC |
|---|---|---|---|---|
| alpha158-linear-csi500 | 2026-07-16 17:08 | 6 | **0.0247**（6 折中 1 折为负 -0.0085） | 0.0295 |
| alpha158-lightgbm-csi500 | 2026-07-16 17:21 | **仅 1 折** | **0.0076** | 0.0296 |

MLflow（`runtime/mlflow.db`）仅 1 个实验 `ashare-research`、1 次运行 `mlflow_recorder`（2026-07-16T09:20Z，FINISHED，lightgbm qlib-benchmark，l2.train 0.986 / l2.valid 0.998）。

关键观察：按 V2_DESIGN.md"Linear 是 sanity baseline"原则，**LightGBM 的 OOS 中位 RankIC（0.0076）显著低于 Linear（0.0247）且只跑了 1 折**——非线性模型尚未证明自己优于线性基线。

### 3.4 Drift / Quality 报告

- `runtime/drift/`、`runtime/quality/`、`runtime/features/`、`runtime/outcomes/` **全部不存在**：无特征面板（panel.parquet）、无 Evidently 漂移报告、无数据质量报告、无 outcomes 汇总。
- 漂移/质量流水线只有代码和测试（54 个用例覆盖），从未在生产数据上运行过。
- 注：V1 metrics 中 `data_quality_passed: true / drift_passed: true` 是基线导出文件的字段，并非来自 research 漂移流水线产物。

### 3.5 数据源状态

- **Qlib 社区数据**：健康检查通过；日历 2018-01-02 → 2026-07-14（2068 天），5537 只标的，158 个 Alpha158 特征；仅限冷启动基准，不可用于晋级。
- **Tushare 生产数据**：**失败**。`runtime/data/meta/last-sync-error.json`：`stock_basic failed after 3 attempts: tenant key expired (contact admin to renew)`（2026-07-16 17:10 +08:00 记录）。`runtime/data/` 下无任何 Parquet 数据。

## 4. 距离晋升还差哪些证据（对照 V2_DESIGN.md "Remaining admission work"）

| # | 准入工作 | 状态 | 缺口 |
|---|---|---|---|
| 1 | 恢复有效 Tushare 凭证并回填 2018 至今数据 | ❌ 阻塞 | 租户密钥过期 7 天未恢复；零生产数据 |
| 2 | Linear/LGBM/DoubleEnsemble 各 ≥6 折生产数据 OOS | ❌ 0 折 | 仅有社区数据折（linear 6 / lgbm 1） |
| 3 | 同日期同成本假设的 V1 基线证据 | ⚠️ 部分 | 基线每日刷新但 `oos_folds=0`，无多折 OOS 记录 |
| 4 | 候选模型 ≥60 交易日、≥20 笔已平仓影子交易 | ❌ 0 | 无账本、无影子账户状态文件 |
| 5 | 可供 Dashboard 对比的候选影子净值曲线 | ❌ 无 | `status.json` 中 `model_health` / `outcome_feedback` 均为 null |

结论：**进化流水线处于"基础设施就绪、证据为零"状态**。代码与测试完备（54 测试全绿），但因 Tushare 凭证过期，整个生产数据链路（特征面板 → 训练 → OOS → 影子交易 → 晋级证据）自 2026-07-16 起完全停摆。V1 仍是唯一生产策略且无 ML challenger。

## 5. 发现的问题清单

1. **【阻断】Tushare 租户密钥过期**（2026-07-16 起，已 7 天）：生产数据同步失败，是全部准入工作的上游阻塞点。需管理员续期后回填 2018 至今数据。
2. **【高】注册表与账本从未初始化**：`runtime/registry/`、`runtime/ledger.db` 均不存在，无候选模型、无影子交易记录，晋级证据链为空。
3. **【高】LightGBM 基准弱于 Linear 基线**：社区数据上 LGBM 中位 RankIC 0.0076（1 折）vs Linear 0.0247（6 折）；且 LGBM 仅 1 折，折数不足。按设计文档原则，复杂模型尚未证伪"不值得"。
4. **【中】V1 回撤恶化、文档数据过时**：V1 最大回撤从设计文档快照的 26.48% 扩大到 -29.09%，换手率升至 3784.5%；V2_DESIGN.md 第 5-6 行数字已陈旧，建议下次文档更新时刷新。
5. **【中】无 drift/quality 运行产物**：Evidently 漂移与质量门禁只有代码从未运行（无特征面板），fail-closed 推理在生产中无实际输入。
6. **【低】表述对齐**：任务/文档中的"hash 链"与实际实现（主键幂等 + 工件 SHA-256）不一致，建议在 README/V2_DESIGN 中明确完整性机制。
7. **【低】技术债**：evidently/litestar 弃用告警 10 条；`status.json` 中 `v1_baseline: null` 与基线文件存在的事实不一致（可能是导出路径未衔接）。
8. **【观察】仓库状态**：research/ 在 git 中干净（分支 `codex/analytics-hardening`，最近提交 fef714f）；工作区另有 pyserver/main.py、web/package*.json 修改及 monitoring/、tmp/ 等未跟踪目录（非本次检查范围，保持原样未动）。

---

*本报告为只读检查产物，未修改 research/ 任何文件，未训练、未调参、未晋级、未触碰注册表与 git。*
