# production-rebuild 合并前真实性审计报告

审计日期：2026-07-29
审计分支：codex/production-rebuild
审计基点：955e4a5ffca6d08538341dbeced09b7fe980da06（archive tag: archive/production-rebuild-20260729）
审计提交：6536b56（阻塞1）→ 535177d（阻塞2）→ 5421ec4（阻塞3）
依据：CC（Opus5/Claude Code）评审意见逐项对照仓库核实；评审无仓库访问权，所有"需核实"项以仓库实况为准。

## 一、存档（已完成）

| 项 | 值 |
|----|-----|
| commit SHA | 955e4a5ffca6d08538341dbeced09b7fe980da06 |
| archive 分支 | archive/production-rebuild-20260729（已推送 origin） |
| 不可变 tag | archive/production-rebuild-20260729（附注 tag，已推送 origin） |
| git bundle | archive/production-rebuild-20260729/production-rebuild-20260729.bundle（2.6MB，verify 通过，含完整历史） |
| 完整 diff | archive/production-rebuild-20260729/full-diff.patch（2.2MB，main...955e4a5） |
| 测试输出 | web-test-output.txt（148 过 + tsc 0 错误）、research-test-output.txt（252 过），sha256 已记入 race-reports-sha256.txt |
| 赛马报告 SHA-256 | archive/production-rebuild-20260729/race-reports-sha256.txt |
| 未提交修改 | 仅 `M .gitignore`（存档目录加入忽略），已单独记录于 uncommitted-changes.txt，未静默提交 |

## 二、阻塞项 1：复权和点时重放确定性

| 核实项 | 状态 | 证据 |
|--------|------|------|
| adj_factor 使用决策日之后数据 | NOT FOUND | features.py:165-166（as_of 过滤在一切计算前）+ :325（close*adj_factor 后复权）；Tushare adj_factor 历史值不被未来除权改写 |
| 用最新 adj_factor 归一化整段历史 | 研究侧 NOT FOUND；web legacy 侧 **CONFIRMED** | pyserver.ts:88-91 fetchKlines 用 qfq（锚点=拉取时最新因子）。影响边界：信号基于收益率不变量故稳定；价格水平漂移影响仓位金额/费用精确复现；仅 legacy dashboard 路径，不进冠军判定/赛马/生产门禁。修复属大改，本阶段记录不修 |
| 历史数据被未来除权事件重写 | 研究侧 NOT FOUND；web 同上 CONFIRMED（有界） | 同上 |
| features/Tide/Prism 不受 as_of 约束的最新数据调用 | NOT FOUND | tide.ts:200-203、prism.ts:243-244 均 rowsAtOrBefore(asOf)；backtest.ts:153-166 latestFundamentalAsOf 按 effective_date<=date；dashboardData.ts:138-153 法定披露滞后保守近似；new Date() 均为元数据 |
| （附加）ST 用当前名称判历史涨跌停 | **PARTIAL** | tradingConstraints.ts:140-145 /ST/i.test(当前名称)；影响 web 引擎历史段阈值近似，池内 ST 标的极少；研究侧用 stk_limit 真实涨跌停价不受影响。记录不修 |
| 重放确定性测试（新增，入 CI） | 完成 | research/tests/test_replay_determinism.py（2 个，1e-9 数值容差+离散列精确一致）；web/test/replay-determinism.test.ts（5 个）。负向验证：删除 as_of 过滤后测试全部失败，还原后通过。CI 现有 web/research job 自动覆盖（ci.yml），无需改 CI |

## 三、阻塞项 2：历史股票池和退市股覆盖

| 核实项 | 状态 | 证据 |
|--------|------|------|
| stock_basic 拉取 L/D/P 全状态 | NOT FOUND（无缺陷） | data_sync.py:801-812 `for status in ("L","D","P")` |
| 2018 至今历史成分中退市股有日线数据 | NOT FOUND | 按 trade_date 整市场拉取（含退市股历史）；实测 1403 历史成分中仅 3 只零数据，且全部 118/118 天全程停牌（sh600485/sz000939/sz002005） |
| 数据缺失静默 continue | NOT FOUND | index_weight 空月 continue 后有缺口/陈旧/成分数/权重和/union=800 硬校验 raise（data_sync.py:405-423）；同步失败记录 SyncFailure |
| 退市/停牌/离池处理规则 | 已核实并成文 | membership 按 [list_date, delist_date] 截断；suspend_d 停牌日不计缺失；coverage_ratio=actual/(expected-suspended)，阈值 0.95 |
| 成分快照只 ffill 无 bfill | NOT FOUND | data_sync.py:452-465（区间=[快照日, 下一快照日前一天]）；:405-409 无快照则 raise |
| 生效日月内前视 | 代码层 NOT FOUND；供应商语义 UNVERIFIED | 代码使用 Tushare 快照实际 trade_date；Tushare 对 index_weight 历史值的回填口径无法从仓库验证（见"未完成事项"） |
| 产出 coverage 审计 | 完成，**PASS** | reports/2026-07-29/historical-universe-coverage.json + .md；CLI `audit-universe-coverage` 严重缺失时 exit 1（MAX_SILENTLY_SKIPPED=0、MAX_GAP_MEMBERS=0）；tests 6 个 |

实测结果：L=5530 / D=339 / P=0；1403 历史成分；expected=1,661,531 成员日、actual=1,651,045、suspended=10,945、missing=35（0.002%）；点时成员数恒 800（26 段全 800）；0 静默跳过；0 覆盖缺口。

## 四、阻塞项 3：生产信号门禁和数据新鲜度

出口全清单见 [production-signal-routes.md](production-signal-routes.md)（16 条路径逐一登记）。

| 核实项 | 状态 | 证据 |
|--------|------|------|
| Champion 缺失回退默认策略 | NOT FOUND | deriveProductionGate 全链路 cash-only；写入点 deployableChampionIds:[] + 非 cash-only throw（build-dashboard.ts:579-583、sync-production-gate.ts） |
| Champion 配置解析失败回退旧策略 | NOT FOUND | readProductionGate 缺失/非法 → cash-only（productionGate.ts:470-484） |
| **数据陈旧显示为正常空仓** | **CONFIRMED → 已修复（5421ec4）** | 修复前 buildProductionSignalsApiPayload 无时间老化检查，老化快照无限期返回 active/stale=false。修复：signal_date 超过 PRODUCTION_SIGNALS_MAX_AGE_DAYS（默认 15 自然日，覆盖春节停牌）→ cash-only + stale=true + PRODUCTION_SIGNALS_STALE；首页同步隐藏可执行信号并显示"数据陈旧，暂停执行"。修复前 3 个测试失败、修复后 13/13 通过 |
| runtime 与代码版本不兼容出信号 | NOT FOUND | 合同 SHA-256 覆盖 10 个代码/配置文件字节（productionGate.ts:412-440） |
| 门禁未通过输出演示买入信号 | NOT FOUND | cash-only 时 signals 恒 []；测试"cash-only API payload can never leak legacy buy signals"守卫 |
| 五状态区分 | 修复后满足 | 正常空仓 / Champion 未通过 / 数据陈旧（PRODUCTION_SIGNALS_STALE）/ 数据源失败（PRODUCTION_SIGNALS_MISSING_OR_MISMATCHED）/ 系统异常（PRODUCTION_GATE_INVALID 等），经 status+stale+reason_codes 区分 |

## 五、新增及修改的测试

新增：
- research/tests/test_replay_determinism.py（2 个）
- web/test/replay-determinism.test.ts（5 个）
- research/tests/test_universe_coverage.py（6 个）
- web/test/production-gate.test.ts 新增 5 个新鲜度测试（修复前 3 个失败、修复后全过；2 个边界守卫防过度阻断）

修改：
- web/test/production-gate.test.ts 既有 1 个 active 断言测试注入固定时钟（消除时间炸弹，行为不变）
- research/ashare_research/universe_coverage.py（新审计模块）、research/ashare_research/cli.py（注册 audit-universe-coverage）

全量结果（HEAD=5421ec4）：web 158/158 过 + tsc 0 错误；research 260/260 过。

## 六、commit 列表（codex/production-rebuild，本地，未推送）

| commit | 内容 |
|--------|------|
| 6536b56 | Add replay-determinism guards for merge blocker 1（阻塞1测试） |
| 535177d | Add historical universe coverage audit for merge blocker 2（阻塞2审计+报告） |
| 5421ec4 | Fail closed on stale production signals for merge blocker 3（阻塞3唯一 CONFIRMED 修复，修复前失败测试在先） |
| （本报告） | Add merge audit reports（audit-summary + replay-determinism-report + production-signal-routes） |

## 七、合并建议

**建议可以合并。** 三个阻塞项均已闭环：阻塞1 研究侧点时安全经测试验证（web qfq 为有界 CONFIRMED，不在冠军/生产链路，已记录）；阻塞2 覆盖审计 PASS 且严重缺失 fail；阻塞3 唯一 CONFIRMED（新鲜度）已按"修复前失败测试→最小修复→全量通过"闭环。未发现需要推翻 Codex 既有实现的审计意见；未为迎合审计意见重写正确代码。

建议合并方式：将 codex/production-rebuild（含 955e4a5 + 4 个审计 commit）合入 main；合并前不需要再改代码。

## 八、尚未完成和无法验证的事项

1. **UNVERIFIED**：Tushare index_weight 历史快照的供应商回填语义（是否点后补当月成分）无法从仓库验证；缓解：代码使用供应商发布的快照实际 trade_date 且无时点错配校验失败记录。
2. web qfq → hfq 统一（CONFIRMED 有界缺陷的根治）：属大改，留待专门阶段。
3. ST 点时名称（namechange 区间接入 web 引擎）：与 2 一并处理。
4. 分钟数据覆盖仍 blocked（3.69%）：既有状态，日频赛马不受影响，非本阶段事项。
5. CC 附件其余问题（本阶段仅记录为 follow-up，未展开）：成本黄金用例、DSR 试验次数、收益集中度、OPS 安全加固、死代码清理（StrategyDetailView/BuySignalHistoryTable）、ops 页陈旧徽标。
6. RQAlpha：本阶段未启动。合并通过后按指定顺序执行：先同源数据对账，再固定信号订单对账，最后 Momentum 策略对账。
