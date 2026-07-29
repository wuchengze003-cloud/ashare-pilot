# 生产信号出口清单（合并阻塞项 3）

审计日期：2026-07-29
分支：codex/production-rebuild（审计基点 955e4a5 + 审计修复 6536b56 / 535177d / 5421ec4）
方法：全仓 grep + 逐文件阅读，覆盖 API 路由、Server Components、daily-close、后台脚本、策略页面、兼容接口、scorer 直接调用点、runtime fallback、缓存/CDN。

## 一、正式信号出口（消费侧）

| # | 出口 | 位置 | 是否经过统一生产门禁 | 说明 |
|---|------|------|----------------------|------|
| 1 | `GET /api/signals` | web/app/api/signals/route.ts | **是** | 唯一程序化信号接口。只读门禁产物：`buildProductionSignalsApiPayload(gate, signals, asOf)`。force-dynamic + `Cache-Control: no-store`（无 CDN 缓存面）。拒绝 forceLive/lookbackDays/minScoreToBuy/maxPositions 参数（400），asOf 格式校验。 |
| 2 | 首页 `/` | web/app/page.tsx | **是** | 仅当 gate active + 快照 active + champion 匹配 + 快照新鲜（5421ec4 起）才展示信号；否则为空。陈旧时显示"信号数据陈旧，暂停执行"而非正常空仓。 |
| 3 | `/signals` 页面 | web/app/signals/page.tsx | 不适用 | 6 行 redirect 到 `/#signals`，无独立信号出口。 |
| 4 | `/dashboard` | web/app/dashboard/page.tsx | **是**（只读展示） | 赛马验收页，只读 gate/信号元数据，不输出买卖指令。 |
| 5 | `/dashboard/[strategyId]` | web/app/dashboard/[strategyId]/page.tsx | 不适用 | redirect 到 /dashboard。StrategyDetailView.tsx 与 BuySignalHistoryTable.tsx 无任何 import 方（死代码），legacy 信号归档无页面出口。 |
| 6 | `/ops` | web/app/ops/page.tsx | **是**（只读展示） | OPS_TOKEN 鉴权（timingSafeEqual，生产环境未配置时 fail-closed）。展示门禁状态、信号日期、回执匹配；不输出可执行信号。 |
| 7 | `GET /api/strategies` | web/app/api/strategies/route.ts | **是** | 只读 gate 投影；`executable = gate active && candidate==champion`；不含信号。 |
| 8 | `GET /api/version` | web/app/api/version/route.ts | **是** | 回显 manifest + gate 快照；无信号。 |

## 二、非正式信号出口（研究/第三方数据，已核实口径）

| # | 出口 | 位置 | 说明 |
|---|------|------|------|
| 9 | `GET /api/backtest` | web/app/api/backtest/route.ts | 内部研究工具：INTERNAL_API_TOKEN 鉴权（生产环境未配置时 fail-closed），用户自定义参数跑 runBacktest。输出自定义回测结果，**不是**正式交易信号，不经过生产门禁（也不需要）。 |
| 10 | `GET /api/analyst` | web/app/api/analyst/route.ts | 第三方分析师一致预期数据（buy_count/buy_ratio，来自 pyserver/数据供应商），非策略信号；符号白名单校验。 |
| 11 | `POST /api/spot/batch` | web/app/api/spot/batch/route.ts | 实时行情快照，无信号。 |
| 12 | `POST /api/universe/refresh` | web/app/api/universe/refresh/route.ts | 独立 token 鉴权；只生成股票池变更提议（applied:false），不写正式池，无信号。 |

## 三、信号写入路径（生产侧）

| # | 写入方 | 位置 | fail-closed 约束 |
|---|--------|------|------------------|
| 13 | `npm run dashboard:update` | web/scripts/build-dashboard.ts:404-407, 573-592 | `deriveProductionGateFromFiles(undefined, { deployableChampionIds: [] })`：Web 侧无可部署实现，门禁恒为 cash-only；line 579-583 若非 cash-only 直接 throw；line 584-592 只写 `buildCashOnlyProductionSignals`（signals 恒为 []）。**永不可能发布买入信号。** |
| 14 | `npm run gate:sync` | web/scripts/sync-production-gate.ts | 同 13：`deployableChampionIds: []` + 非 cash-only throw。 |
| 15 | legacy 信号归档 | web/scripts/build-dashboard.ts:573,690 → web/lib/signalHistory.ts | momentum-v1 等旧策略诊断归档，写 `signals-history/` 与 `strategies/<id>/history/`；不可变冲突检测（line 91-95）；**无公开读取出口**（唯一读者 StrategyDetailView 为死代码）。 |
| 16 | daily-close 控制面 | web/scripts/daily-close.ts | 部署期新鲜度强约束：benchmark 最新日 ≠ 预期日 → 置 stale-or-no-session 并 throw（line 356-361）；`validateDailyCloseData` 要求 production signal_date/latest_complete_date == 预期日（dailyClose.ts:263-274）；部署后回测本地+远程 `/api/signals`（validateSignalsEndpointBody：日期匹配、cash-only 不得带 champion/信号）。任一步失败整体失败，不发布。 |

## 四、runtime fallback 与缓存面

| 路径 | 行为 | 结论 |
|------|------|------|
| `readProductionGate`（productionGate.ts:470-484） | 文件缺失 → cash-only + `PRODUCTION_GATE_MISSING`；JSON/schema 非法 → cash-only + `PRODUCTION_GATE_INVALID` | fail-closed ✅ |
| `readProductionSignals`（productionGate.ts:486-503） | schema/status/字段校验失败 → null → API 层按 `PRODUCTION_SIGNALS_MISSING_OR_MISMATCHED` 处理 | fail-closed ✅ |
| `deriveProductionGate`（productionGate.ts:231-387） | champion 缺失/合同哈希不匹配/schema 不兼容/分钟覆盖不足/无部署实现 → cash-only | fail-closed ✅ |
| 合同哈希（productionGate.ts:412-440） | 覆盖 production-race-v2.json + cost-model.json + trading-constraints.json + 7 个研究侧 py 文件字节；任一文件缺失 → 哈希不可用 → 门禁不匹配 → cash-only | runtime 与代码版本不兼容时不得出信号 ✅ |
| 缓存/CDN | /api/signals no-store；所有相关页面 force-dynamic；next.config.js 无缓存头覆盖 | 无陈旧缓存面 ✅ |
| scorer 直接调用点 | grep 全仓：momentum/tide/prism scorer 仅被 legacy dashboard 构建脚本与内部 /api/backtest 引用；无公开出口 | ✅ |

## 五、五状态区分能力（/api/signals 与首页）

| 状态 | 接口表现 | reason_codes |
|------|----------|--------------|
| 正常空仓 | `status=cash-only, stale=false, signals=[]` | 门禁原因（如 `NO_PRODUCTION_CHAMPION`） |
| Champion 未通过 | `status=cash-only, signals=[]` | `NO_PRODUCTION_CHAMPION` / `CHAMPION_*_MISMATCH` / `RACE_CONTRACT_MISMATCH` / `MINUTE_DATA_INCOMPLETE` 等 |
| 数据陈旧 | `status=cash-only, stale=true, signals=[]` | **`PRODUCTION_SIGNALS_STALE`（5421ec4 新增）** |
| 数据源失败 | `status=cash-only, stale=true, signals=[]` | `PRODUCTION_SIGNALS_MISSING_OR_MISMATCHED`（文件缺失/schema 非法）；门禁缺失时另有 `PRODUCTION_GATE_MISSING` |
| 系统异常 | `status=cash-only, signals=[]` | `PRODUCTION_GATE_INVALID` / `RACE_CONTRACT_STALE` 等 |

## 六、核实结论

| 审计要求 | 状态 | 证据 |
|----------|------|------|
| Champion 缺失时不得回退默认策略 | NOT FOUND（无绕过） | productionGate.ts:231-387；写入点 deployableChampionIds:[] 且非 cash-only throw（build-dashboard.ts:579-583） |
| Champion 配置解析失败不得回退旧策略 | NOT FOUND | readProductionGate 非法 → cash-only（productionGate.ts:470-484） |
| 数据陈旧时不得显示为正常空仓 | **CONFIRMED → 已修复（5421ec4）** | 修复前：buildProductionSignalsApiPayload 无时间老化检查，老化快照无限期返回 active/stale=false。修复前失败测试 3 个，修复后 13/13 通过 |
| runtime 与代码版本不兼容不得出信号 | NOT FOUND | 合同 SHA-256 覆盖 10 个代码/配置文件（productionGate.ts:412-440） |
| 门禁未通过不得输出演示买入信号 | NOT FOUND | cash-only 时 signals 恒 []（buildCashOnlyProductionSignals，productionGate.ts:505-527；测试"cash-only API payload can never leak legacy buy signals"） |
| 五状态区分 | NOT FOUND（修复后满足） | 见第五节 |

## 七、残余说明（非阻塞，记录）

- `/ops` 页面"生产信号日"展示的是快照原始 signal_date，未加陈旧徽标；该页为鉴权运维诊断页，不输出可执行信号，不视为信号出口缺陷。
- StrategyDetailView.tsx / BuySignalHistoryTable.tsx 为无引用死代码，建议后续清理（本阶段不做无关重构）。
- legacy `signals-history/` 归档目前无公开出口；若未来重新暴露，必须先标称为"旧版诊断"并经过与首页一致的门禁/新鲜度检查。
