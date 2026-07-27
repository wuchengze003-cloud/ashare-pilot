# web/ 目录全面检查报告

- 检查人：Web_检查员_A1
- 日期：2026-07-23
- 范围：`web/`（Next.js 15 App Router）—— lib/ 关键模块、app/api 路由输入校验、测试与类型检查
- 运行环境说明：本机默认 Node 为 v24.15.0，`better-sqlite3@11.10.0` 在 Node 24 下无预编译二进制且源码编译失败（gyp make exit 2），`npm ci` 会中途失败并留下空的 `node_modules`。本次全部命令改用系统 Node v22.22.x 执行成功。**建议尽快处理（见 M1）**。

## 基线与最终结果

| 项目 | 基线 | 修复后 |
|---|---|---|
| `npm test`（node --test --import tsx） | 78/78 通过 | **89/89 通过**（新增 11 个测试） |
| `tsc --noEmit` | 通过（0 错误） | **通过（0 错误）** |
| 双时区回归（TZ=America/New_York） | — | 89/89 通过 |

## 已修复项清单（低风险，直接修复并补测试）

| # | 文件:行 | 问题 | 修复 |
|---|---|---|---|
| F1 | lib/concurrent.ts:16 | `mapPool` 在 `limit<=0` 或 `NaN` 时 `Array.from({length: NaN})` 产生 0 个 worker，**静默返回全 `undefined` 数组**，调用方（回测/信号加载）会误判为"全部无数据" | worker 数下限钳制为 1，非有限值回退 1；新增 `test/concurrent.test.ts` 用例覆盖 0/-3/NaN |
| F2 | app/api/backtest/route.ts:22-46 | POST 体完全未校验：`req.json()` 抛错、缺 `endDate` 时 `replaceAll` 抛 TypeError、非法日期产生 `NaN-NaN-NaN` 传给 pyserver，均在流式 try/catch 之外变成不透明 500 | 新增 `lib/backtestConfig.ts` 纯函数校验模块（日期 ISO 且真实存在、start<=end、数值边界、整数约束、布尔类型），路由先校验再开流，非法输入返回 400；新增 `test/backtest-config.test.ts`（8 个用例） |
| F3 | app/api/signals/route.ts:195 | `asOf` 查询参数未校验，任意字符串（如 `../../x`）会进入日期运算与 `loadActiveEntries` 的字符串比较 | `isIsoDateString` 校验，非法返回 400 |
| F4 | app/api/universe/refresh/route.ts:10-12 | 内部令牌用 `provided !== token` 普通字符串比较，存在时序侧信道 | 改用 `lib/apiSecurity.ts` 新导出 `hasConfiguredTokenAccess`（`timingSafeEqual`），保持"未配置令牌即拒绝"的 fail-closed 语义 |
| F5 | lib/apiSecurity.ts | 缺通用校验原语 | 新增 `isIsoDateString`（拒绝 `2026-02-30` 等不可能日期）与 `hasConfiguredTokenAccess`；`test/api-security.test.ts` 新增 2 个用例 |
| F6 | lib/dashboardData.ts:129-142 | `effectiveDateForReport` 用本地时区 `new Date(y,m,d)` 构造却配合 `setUTCDate/getUTCDate` UTC setter，**非 UTC 时区下财报生效日偏移一天**，直接影响回测的 point-in-time 基本面（look-ahead 防线） | 改用 `Date.UTC(y,m,d)` 构造并导出该函数；`test/dashboard-data.test.ts` 新增披露滞后用例（年报/半年报/Q1/Q3），并在两个时区下回归通过 |

## 问题清单（记录，未改 —— 中/高风险或需架构决策）

| # | 严重度 | 文件:行 | 说明 | 建议 |
|---|---|---|---|---|
| M1 | **高（环境）** | web/package.json | Node 24 下 `better-sqlite3@11.10` 无法安装：无预编译二进制，源码编译失败；`npm ci` 先清空 `node_modules` 再失败，破坏性是"依赖全失"。任何用 Node 24 的部署/新机器都会踩中 | 短期：`package.json` 加 `"engines": {"node": ">=22 <24"}` 并在 README 注明；中期：升级 `better-sqlite3@^12`（支持 Node 24）并全量回归 |
| M2 | 中 | lib/cache.ts:167-191 | `listBacktestResults` 对 `winRatePct`/`turnoverPct` 硬编码 0 —— 历史回测列表页的胜率/换手统计全部失真 | `backtest_results` 表 `ALTER TABLE` 补两列（旧行回填 0），或列表查询改为解析 `result_json` 的 stats（代价是 IO） |
| M3 | 中 | lib/universe-refresh.ts:162-179 | reclassify 只改 `theme`，不写 `previous_theme`/`theme_effective_from`；而 `resolveEntryAsOf` 依赖这两字段做 point-in-time 还原 —— 一旦刷新路由写入，历史回测的主题标签被追溯改写 | reclassify 时同时落 `previous_theme=旧值`、`theme_effective_from=生效日`（需处理已有 previous_theme 的链式迁移）；注意刷新路由目前是"只提议不写入"，风险在写入路径启用后兑现 |
| M4 | 中 | lib/concurrent.ts:9-18 | `mapPool` 一个 item reject 后其余 worker 继续后台跑，多 item 失败时后续 rejection 可能成为 unhandledRejection | 本次只修了下限钳制；容错场景建议改 `Promise.allSettled` 聚合或 per-item catch 收集错误统一抛出（语义变更，需评估全部调用方） |
| M5 | 低 | lib/backtest.ts:541,567 | 买卖股数统一按 100 股整手；科创板（688）实为最低 200 股、以上按 1 股递增，小资金回测有轻微失真 | 按板块实现 lot 规则（`priceLimitFraction` 已有板块判断可复用） |
| M6 | 低 | lib/backtest.ts:573-575 | `avgCost` 不含买入手续费，且部分减仓保留原成本基准；胜率（winRatePct）口径偏乐观 | 口径在报告中注明，或将费用摊入成本基准 |
| M7 | 低 | lib/runtimeData.ts:14-16 | `runtimeDataPath(name)` 不防 `../` 路径穿越；当前调用方均为硬编码常量 | 若未来 name 来自外部输入，加 basename 白名单校验 |
| M8 | 低 | lib/pyserver.ts:45-51 | inflight 去重在 settle 后保留 100ms，期间同一 key 的 rejection 会被复用（瞬时失败放大） | 可接受；如需严格可在 finally 里立即删除失败条目 |
| M9 | 低 | lib/cache.ts:99-125 | `cached()` 对 fetcher 返回 `null` 的结果会写入缓存但下次仍判 miss（`JSON.parse("null")===null`），null 结果不享受缓存 | 如确需缓存空结果，加 sentinel 包装 |
| M10 | 低 | lib/dashboardData.ts:78 | `parsePriceCsv` 朴素 `split(",")`，名称字段含逗号会错位 | 数据源 CSV 当前无此情况；如需健壮性换用 CSV 解析器 |

## API 路由输入校验结论

- `app/api/spot/batch/route.ts`、`app/api/analyst/batch/route.ts`：`symbols` 经 `parseAshareSymbols` 严格校验（6 位数字、去重、上限 100、413 截断）——合规。
- `app/api/analyst/route.ts`：symbol 校验 + 双段超时兜底——合规。
- `app/api/backtest/route.ts`：**已修复**（F2），另有内部令牌门禁。
- `app/api/signals/route.ts`：**已修复**（F3）；自定义参数触发实时计算时需内部令牌——设计合理。
- `app/api/universe/refresh/route.ts`：**已修复**（F4）。

## 验证命令与结果

```bash
# Node v22.22.x（Node 24 会因 better-sqlite3 编译失败）
npm ci          # 成功，0 vulnerabilities
npm test        # 89/89 通过（基线 78，新增 11）
./node_modules/.bin/tsc --noEmit   # 0 错误
TZ=America/New_York npm test       # 89/89 通过（验证 F6 时区修复）
```

## 备注

- 未触碰 `web/data/universe.json`、`research/` 注册表等禁区文件；未做任何 git commit/push。
- 工作区中 `pyserver/main.py`、`web/package.json`（overrides 调整）存在非本次检查产生的既有改动（疑似并行任务），本次未涉及。
