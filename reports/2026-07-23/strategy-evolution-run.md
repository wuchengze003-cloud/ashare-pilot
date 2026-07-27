# 策略进化推进记录 · 管线就绪化（数据断供下）

- 执行人：策略进化_工程师_B2
- 日期：2026-07-23（本地，Asia/Shanghai）
- 上游输入：`reports/2026-07-23/research-check.md`、`reports/2026-07-23/pyserver-check.md`
- 硬约束遵守声明：未训练任何参数、未晋升任何模型、未创建/修改 `active_model.json` 与注册表内容、未触碰 `web/data/universe.json` 与 V1 信号历史、未 git commit、未输出任何密钥值。唯一对 runtime 的写入：`ledger.db` 初始化（任务明确允许的运维操作）与 `data/meta/last-sync-error.json`（CLI 自身错误记录行为）。

---

## 1. 数据断供范围诊断

### 1.1 research 数据管线读哪里的 token

`ashare_research/data_sync.py::build_tushare_client` 从环境变量读取 `TUSHARE_TOKEN` / `TUSHARE_HTTP_URL`；CLI `data-sync` 的 `--env` 默认值硬编码为 **`pyserver/.env`**（`cli.py:575`）。已核实 `pyserver/.env` 存在且含 `TUSHARE_HTTP_URL`、`TUSHARE_TOKEN` 两个键（仅列键名，未读值）。

**结论：research 与 pyserver 共用同一份代理密钥，断供是同一根因。** research 没有独立的凭证或独立数据源。

### 1.2 今日实测：断供仍在持续

```
cd research && uv run ashare-research data-sync --start 2026-07-20 --end 2026-07-22
→ RuntimeError: stock_basic failed after 3 attempts: tenant key expired (contact admin to renew)
```

`runtime/data/meta/last-sync-error.json` 已刷新为今日证据（`recorded_at: 2026-07-23T16:18:20+08:00`）。失败发生在第一步参考表 `stock_basic`，即连日历/股票清单都拿不到，`runtime/data/` 下仍然零 Parquet。

### 1.3 东财/腾讯/easy-tdx fallback 能否作为研究数据导入？——不能

| 判定维度 | 事实 |
|---|---|
| fallback 覆盖字段 | pyserver 的 easy-tdx / Eastmoney / 腾讯 / 新浪链路只产出 K 线（OHLCV）、spot、基本面快照（pe/pb/市值） |
| 特征面板硬依赖 | `features.py`：`adj_factor` 缺失即 `FileNotFoundError`（必需）；`stk_limit` 缺失即报错（可执行标签必需）；`daily` 必需；`daily_basic`（换手率/估值/市值）、`moneyflow`、`suspend_d` 为可选但参与特征 |
| 点时性 | Tushare 按 `trade_date` 分区快照是 point-in-time 语义；fallback 是实时/复权后接口，无前复权因子历史快照与涨跌停价历史，无法重建 PIT 面板 |
| 晋升语义 | `training.py:421` 硬编码 `data_source="tushare-pro-point-in-time"`，晋升门控要求 validated Tushare 数据；替代源产物即使生成也只能标 `promotable: false` |

结论：**fallback 连"管线验证"都不够格**——缺少 `adj_factor` 与 `stk_limit` 两个硬依赖，任何替代导入都需要伪造字段，产出非点时数据，违反约束。本任务未做任何伪造式导入。

## 2. 执行步骤与结果

| # | 命令 | 结果 |
|---|---|---|
| 1 | `uv run pytest -q` | **54 passed**, 0 failed，4.49s（与 A3 检查一致，环境未退化） |
| 2 | `uv run ashare-research ledger-init`（连跑 2 次） | 创建 `runtime/ledger.db`；第二次重跑无错误——`CREATE TABLE IF NOT EXISTS` 幂等成立 |
| 3 | `uv run ashare-research health` | 依赖 8/8 可用；`ledger: {predictions:0, decisions:0, executions:0, outcomes:0}` 四表空账本就绪；registry 返回默认空状态（未落盘，`runtime/registry/` 仍不存在，保持原样） |
| 4 | `data-sync --start 2026-07-20 --end 2026-07-22` | 失败于 `stock_basic`：tenant key expired（证据见 §1.2） |
| 5 | `run-challenger --model-type lightgbm --optuna-trials 5`（fail-closed 验证） | 在第一步 `build_feature_panel` 即抛 `FileNotFoundError: daily parquet not found`，**未写任何产物**（`runtime/features/` 未创建、注册表零变动）。管线对缺数据正确 fail-closed，不会用空数据静默训练 |

**未跑真实 challenger**：点时数据不可用且替代源不成立（§1.3），按任务约束不强行训练。训练→OOS→shadow 注册链路因此停留在"代码就绪、数据阻断"状态。

### 约束的代码级核实（非仅口头声明）

- **2026 春节后窗口不参训**：`training.py` 冻结 holdout 自 2026-02-24 起，调参合格日期集 `eligible = dates < holdout_start`（line 57），holdout 仅作 test 预测（fold=-1）。窗口隔离是代码强制的。
- **shadow-only 注册**：`run_challenger_experiment` 的终点是 `register_candidate`（加入 `candidates` 列表，summary 状态 `"registered-for-shadow"`），不含 `promote` 调用；晋升另有独立命令与证据门控。

## 3. 当前进化状态快照

| 项 | 状态 |
|---|---|
| 代码/测试 | 就绪（54 测试全绿，依赖 8/8） |
| 账本 `ledger.db` | ✅ 本次初始化完成，四表空，幂等可重跑 |
| 注册表 | 空（默认状态），未初始化目录——留待首个真实 shadow 注册时由 `register_candidate` 自动创建，本次不动 |
| Champion / Challenger | 无（生产仍为 V1 规则策略） |
| 特征面板 / 训练 / OOS / 影子交易 | ❌ 全部阻断于 Tushare 数据断供 |
| V1 基线 | 每日刷新正常（A3 报告：Sharpe 1.824，回撤 -29.09%），未触碰 |

## 4. 阻断项清单

| # | 阻断项 | 解除条件 |
|---|---|---|
| B1 | **Tushare 租户密钥过期**（7-16 起，今日实测仍在） | 管理员续期代理租户密钥（或改用官方直连 token），更新 `pyserver/.env` 的 `TUSHARE_TOKEN`（及 `TUSHARE_HTTP_URL` 若走代理）。research 无需任何改动，自动共用 |
| B2 | 2018 至今生产数据零回填 | B1 解除后跑 `data-sync --start 2018-01-01 --end <当日>`（全量约 2000+ 交易日 × 7 端点，注意限速与重试；`assert_production_dataset` 要求 ≥1500 完整交易日） |
| B3 | 首个 shadow 候选注册 | B2 完成后 `run-challenger`（Linear/LGBM/DoubleEnsemble 各 ≥6 折 OOS 为准入要求） |
| B4 | 晋升证据链（≥60 交易日、≥20 笔影子交易等） | B3 之后逐日积累，无捷径 |

**Tushare 续期需要什么**：向代理服务提供方申请 tenant key 续期（错误原文 "tenant key expired (contact admin to renew)"），或改用 Tushare 官方积分账号直连。只需更新 `pyserver/.env` 一个文件，web、pyserver、research 三处同时恢复。注意 A2 报告提示官方端点同样校验失败，需先确认账号本身的有效性。

## 5. 给 Codex 的验收要点

1. **约束合规**：全任务仅 2 处 runtime 写入（ledger.db 初始化、CLI 自动记录的 last-sync-error.json）；`git status` 中 research 代码文件零改动；注册表/active_model.json/universe.json 未触碰；无训练、无晋升、无 commit。
2. **幂等性证据**：ledger-init 连跑两次成功且 health 显示四表为零——账本初始化可安全并入每日运维。
3. **fail-closed 证据**：缺数据时 `run-challenger` 在特征面板构建第一步即报错且零副作用，不存在"用替代源/空数据静默训练"的路径。
4. **断供诊断闭环**：research 与 pyserver 共用 `pyserver/.env` 凭证（`cli.py:575` 默认 `--env`），续期只需改一处；东财/腾讯 fallback 因缺 `adj_factor`/`stk_limit` 硬依赖不可用于研究管线（§1.3 表格）。
5. **恢复路径明确**：B1→B2→B3→B4 顺序无并行捷径；B2 回填是唯一重活，建议续期后立即启动并挂监控。
6. **遗留观察**（非本次职责，供排期）：注册表目录的首次创建依赖首个候选注册，若希望"空注册表也落盘"需 Codex 决策是否允许该运维操作；`status.json` 的 `v1_baseline: null` 与基线文件不衔接问题仍开放（A3 报告 #7）。

---

*本报告由策略进化_工程师_B2 产出。除 §0 声明的两处 runtime 写入外，未修改 research/ 任何代码或配置，未训练、未晋升、未触碰注册表与 git。*
