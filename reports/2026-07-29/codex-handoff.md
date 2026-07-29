# Codex 工作接续文档：production-rebuild 审计完成与合并

接续日期：2026-07-29
编写：Qoder（审计执行方）→ Codex（production-rebuild 原作者，接续方）

---

## 一、当前状态速览

| 项 | 值 |
|----|-----|
| 仓库 | git@github.com:wuchengze003-cloud/ashare-pilot.git |
| main | 71a355b（origin/main 同步） |
| 工作分支 | codex/production-rebuild = main + 你的 5 个 commit + Qoder 的 4 个审计 commit |
| 你的 5 个 commit | d4db4d5（scorer 点时数据）→ c458ea7（统一成本风险约束）→ 9ee9d62（CSI800 点时管线）→ 571bb6a（预注册赛马框架）→ 955e4a5（生产门禁接入 web/daily-close） |
| Qoder 的 4 个审计 commit | 6536b56（阻塞1 重放确定性测试）→ 535177d（阻塞2 覆盖审计）→ 5421ec4（阻塞3 新鲜度 fail-closed 修复）→ 804f1a2（审计报告） |
| 本地/远程 | 4 个审计 commit **仅在本地**，未推送；main 未合并。等用户最终指令 |
| 存档 | 955e4a5 已存档：分支+tag `archive/production-rebuild-20260729`（已推送 origin）、git bundle、full-diff.patch、测试输出、赛马报告 SHA-256，位于 `archive/production-rebuild-20260729/`（gitignored） |
| 测试 | web 158/158 + tsc 0 错误；research 260/260。CI（.github/workflows/ci.yml）web/research 两个 job 覆盖全部新增测试 |
| 审计结论 | **三个合并阻塞项全部闭环，Qoder 建议合并**。详见 reports/2026-07-29/audit-summary.md |

---

## 二、production-rebuild 冻结后 Qoder 做了什么

1. **为你准备 CC 评审提示词**：项目背景、技术栈、5 commit 摘要、既定前提（数据边界/资金基准/无冠军即空仓）、RQAlpha 计划、6 个评审问题、验证命令，供用户在 Claude Code 中发起评审。
2. **收到 CC（Opus5/Claude Code）审计意见**：CC 无仓库访问权，全部条目基于变更描述给出"定位+判定标准+验证方法"，标注"需核实"。用户明确要求：**不能把审计意见直接当已确认 Bug，也不能为迎合审计重写正确代码，先逐项核实再最小修复**。
3. **完整存档你的成果**（上表），未提交修改仅 `M .gitignore`（存档目录），已单独记录，未静默提交。
4. **逐项核实 + 三个合并阻塞项审计**（见第三节），新增 18 个测试（research 8 + web 10），做了负向验证（故意破坏点时约束，测试必须失败），只修了一个真正的问题（运行时无新鲜度检查）。
5. **产出审计报告**：audit-summary.md / replay-determinism-report.json / historical-universe-coverage.json+.md / production-signal-routes.md。
6. **结论**：你的实现经受住了逐条对照审计。CC 最担心的两条（复权因子泄漏、退市股幸存者偏差）在研究侧均 **NOT FOUND**；唯一 CONFIRMED 的阻塞级问题是运行时信号新鲜度（CC 第 3.6 条，他预判的"最实际风险"），已按"修复前失败测试→最小修复→全量通过"闭环。

---

## 三、CC 审计意见逐项整理（核实状态 + 处置）

状态定义：CONFIRMED（确认存在）/ NOT FOUND（确认不存在）/ PARTIAL（部分存在）/ UNVERIFIED（无法验证）。

### 1. 正确性（前视偏差）

| CC 条目 | 核实状态 | 证据 | 处置 |
|---------|---------|------|------|
| 1.1 复权因子全局归一化泄漏（CC 列为阻塞#1） | 研究侧 **NOT FOUND**；web legacy 侧 **CONFIRMED（有界）** | features.py:165-166 as_of 过滤在一切计算之前，:325 用 close*adj_factor 后复权（Tushare adj_factor 历史值不被未来除权改写）。web/lib/pyserver.ts:88-91 fetchKlines 用 qfq（锚点=拉取时最新因子），但仅 legacy dashboard 路径，信号基于收益率不变量故稳定，**不进冠军判定/赛马/生产门禁** | 研究侧：按 CC 建议补了重放确定性测试（6536b56）并做负向验证（删 as_of 过滤→测试失败）。web qfq 根治属大改，记录为 follow-up |
| 1.2 股票池缺退市股（CC 列为阻塞#2） | **NOT FOUND** | data_sync.py:801-812 显式拉取 L/D/P 三状态；daily 按 trade_date 整市场拉取（含退市股历史）；覆盖审计实测：1403 历史成分仅 3 只零数据且全部 118/118 天全程停牌；0 静默跳过、0 覆盖缺口 | 535177d 新增 universe_coverage.py 审计模块 + CLI（严重缺失 exit 1）+ 6 个测试 + 报告。**注意：因数据本无缺陷，未重跑终验，预注册未被消耗第二次，CC 第 2.3 条担心的 N+1 情形没有发生** |
| 1.3 CSI800 点时成分生效日/bfill/成分数 | 代码层 **NOT FOUND**；供应商语义 **UNVERIFIED** | data_sync.py:452-465 区间=[快照日, 下一快照日前一天]（半开、纯 ffill）；:405-409 无快照 raise（无 bfill）；union 恒=800 硬校验；实测 26 个点时区间成分数全部=800（CC 要的直方图已在覆盖报告里） | UNVERIFIED 项：Tushare index_weight 历史快照的回填口径无法从仓库验证（代码用的是供应商发布的快照实际 trade_date） |
| 1.4 features.py 危险模式 grep | **NOT FOUND** | 无 shift(-1)/bfill/center=True/全样本 zscore；横截面中位数按日 groupby（点时安全）；label 用当日横截面 median；财务数据 web 侧 latestFundamentalAsOf 按 effective_date<=date + 披露滞后保守近似 | 整个特征面板由重放测试兜底 |
| 1.5 涨跌停与 ST 点时性 | 研究侧 **NOT FOUND**；web 侧 **PARTIAL** | 研究侧用 stk_limit 真实涨跌停价。web/lib/tradingConstraints.ts:140-145 用**当前**名称 /ST/i 判历史涨跌停阈值（CC 指出的反向幸存者偏差模式），但池内 ST 标的极少且不进冠军链路 | 记录为 follow-up（与 qfq 统一一起修） |
| 1.6 tide.ts/prism.ts 点时性 | **NOT FOUND** | tide.ts:200-203、prism.ts:243-244 全部 rowsAtOrBefore(asOf)；全仓 new Date() 均为元数据时间戳，不参与决策；时区敏感路径用 Intl 显式 Asia/Shanghai | web 侧 5 个重放测试兜底。CC 建议的 TZ=UTC vs Asia/Shanghai 对照测试未加（可作小 follow-up） |
| 1.7 快照写一次不可变 | **PARTIAL** | features.py:656-667 原子写（临时文件→replace）；signalHistory.ts:91-95 不可变冲突检测。fsync(dir)/O_EXCL 级加固未逐项验证；CC 建议的追加式修订（r0/r1 + revisions.log）未实现 | 记录为 follow-up（非阻塞） |

### 2. 赛马可信度

| CC 条目 | 核实状态 | 处置 |
|---------|---------|------|
| 2.2 冻结段太短，1.83 不构成证据 | 认可（方法论意见，无需核实） | follow-up：报告措辞修订（标注 SE≈0.8、不作为通过依据）。用户禁止本阶段改门槛/重跑，仅记录 |
| 2.3 DSR 试验次数 N 是否含全部试验；97.5% 阈值预注册时间戳 | **UNVERIFIED**（未展开） | follow-up。注意上文：阻塞2 核实为 NOT FOUND，未重跑终验，无新增试验 |
| 2.4 收益集中度（剔除 top-5 日收益、交易笔数、在市天数占比） | 未展开 | follow-up。交易笔数已在赛马报告（如 prism OOS 29 笔） |
| 2.5 合同哈希覆盖范围 | **PARTIAL** | productionGate.ts:412-440 合同 SHA-256 覆盖 10 个代码/配置文件**字节**（策略代码+成本+约束+特征/组合/模拟器 py），满足"文件流而非清单文本"；但未覆盖：特征面板数据哈希、原始快照哈希、uv.lock/package-lock、Python/Node 版本。记录为 follow-up |

### 3. 生产门禁

| CC 条目 | 核实状态 | 证据 | 处置 |
|---------|---------|------|------|
| 3.1 并行出口（/api/backtest、RSC 直接 import 策略模块等） | **NOT FOUND** | 全仓盘点 16 条路径（production-signal-routes.md）：无 /api/positions；scorer 仅被 legacy 构建脚本与内部鉴权 /api/backtest 引用；StrategyDetailView/BuySignalHistoryTable 是无引用死代码，legacy 信号归档无公开出口 | — |
| 3.2 fail-closed（禁止 ?? DEFAULT_STRATEGY） | **NOT FOUND** | champion 缺失/配置解析失败/合同不匹配/schema 不兼容 → 一律 cash-only；两个写入点 deployableChampionIds:[] 且非 cash-only 直接 throw，**永不可能发布买入信号** | — |
| 3.3 Next.js 缓存 | **NOT FOUND** | /api/signals force-dynamic + Cache-Control: no-store；相关页面全部 force-dynamic；next.config.js 无缓存头覆盖 | — |
| 3.4 OPS_TOKEN 安全 | 主体 **NOT FOUND** | apiSecurity.ts timingSafeEqual ✓；无 NEXT_PUBLIC_ 前缀 ✓；ops_token 走 cookie 非 query ✓；/ops 是 Server Component 服务端鉴权，无配套 /api/ops 路由（无 matcher 漏洞面） | noindex/sitemap 未验证 → 小 follow-up |
| 3.5 daily-close 工作流 | **NOT FOUND** | daily-close.ts 不能指定策略名；champion 配置不可由其写入；部署期强制 signal_date==预期日，否则整体失败不发布 | — |
| 3.6 **新鲜度检查缺失（CC 预判"最实际风险"）** | **CONFIRMED → 已修复（5421ec4）** | 修复前：buildProductionSignalsApiPayload 的 stale 仅反映快照不匹配，无时间老化检查——daily-close 停摆后老化快照无限期返回 active/stale=false，正是 CC 描述的"系统死了你以为在正常空仓" | 最小修复：signal_date 超 PRODUCTION_SIGNALS_MAX_AGE_DAYS（默认 15 自然日，覆盖春节最长停牌，env 可配）→ cash-only + stale=true + PRODUCTION_SIGNALS_STALE；首页显示"信号数据陈旧，暂停执行"而非正常空仓。修复前 3 个测试失败、修复后 13/13 通过 |

### 4. 代码质量 / 安全

| CC 条目 | 状态 | 处置 |
|---------|------|------|
| 4.1 数据管线（限速分桶/重试分类/WAL/断点续传游标） | 未逐项展开（非阻塞项） | follow-up。已核实部分：同步失败记录 SyncFailure 非静默；覆盖审计兜底缺口 |
| 4.2 成本模型双实现黄金用例（CC"无论如何都要补"） | 未展开 | follow-up 首位。现状：cost-model.json + trading_constraints.py 同被合同哈希锁定（字节级），但无 TS/Python 共读的 fixtures/cost_cases.json |
| 4.3 安全（pyserver bind/CORS/token 历史/日志） | 未展开 | follow-up。token 在 .env（gitignored），git 历史未扫描 |

### 5. RQAlpha 意见

CC 的核心主张与用户指令一致且已被采纳：**先同源数据对账（把你的 Tushare 面板转成 RQAlpha bundle，逐格比对收盘价/复权因子/停牌日/涨跌停价），再固定信号订单对账，最后才做 Momentum 策略对账**；RQAlpha 定位为永久独立交叉验证器而非主裁判；只存在于 research/，不进 web 生产依赖树。本阶段未启动，等合并通过后执行。

### CC 的合并结论对照

CC 结论"修改后合并"，阻塞项为 #1/#2/#3。核实结果：#1 研究侧 NOT FOUND（+重放测试）、#2 NOT FOUND（+覆盖审计 PASS）、#3 唯一 CONFIRMED（新鲜度）已修复。**Qoder 据此给出"可以合并"建议，与 CC 的判负方向一致：无冠军结论不受影响（数据无缺陷，不存在"修复后重跑"）。**

---

## 四、GPT 网页版审计意见

**尚未取得。** 此前为让 GPT 网页版评审，仓库已改为公开（见当时会话），但 GPT 的审计输出留在用户的 ChatGPT 对话里，从未粘贴进本仓库或 Qoder 会话，Downloads/桌面也无对应文件。待用户提供原文后，按第三节相同格式（条目→核实状态→证据→处置）整理并更新合并建议。

---

## 五、待 Codex 接续的事项（按优先级）

1. **合并执行**（等用户拍板）：将 codex/production-rebuild 的 9 个 commit（你的 5 个 + 审计 4 个）合入 main。合并前无需再改代码。
2. **RQAlpha 三阶段对账**（用户指定顺序，合并后启动）：
   - 阶段一：数据适配层 + 纯数据对账（收盘价/复权因子/停牌日/涨跌停价逐格比对，硬阈值进 CI）
   - 阶段二：固定信号订单对账（按 (trade_date, code, side) 全外连接，未匹配=0；成交股数完全相等；日权益相对误差<1e-8；现金绝对误差<0.01 元；拒单原因对照表 100% 映射）
   - 阶段三：Momentum-V1 策略对账。RQAlpha 侧关闭内置滑点、注入本项目成本模块
   - 验收机器化：reconciliation_report.json + 差异明细 CSV，越界 CI 失败
3. **follow-up 清单**（不阻塞合并，按序推进）：
   - 成本黄金用例 fixtures/cost_cases.json（TS/Python 共读）+ A 股细则边界测试（资金 T+0、余股一次性卖出、历史费率点时化、整手、缓冲带）
   - web qfq → hfq 统一 + ST 点时名称（namechange 区间）一并处理
   - 赛马报告措辞修订（冻结段 SE≈0.8 标注）+ 收益集中度补充分析 + DSR 试验计数与阈值时间戳核实
   - 合同哈希扩面（数据面板哈希、lockfile、运行时版本）
   - OPS 小项：/ops noindex、TZ 对照测试、快照 fsync/O_EXCL 加固与追加式修订机制
   - 死代码清理（StrategyDetailView.tsx / BuySignalHistoryTable.tsx）
   - pyserver 安全核查（bind/CORS/token 历史扫描）

## 六、约束（用户明确禁止，接续期间持续有效）

- 不修改首页/Dashboard 等公开产品页面的设计（新鲜度修复的三行状态文案是审计要求的安全修复，除外）
- 不恢复旧策略为正式生产信号；门禁未通过不得出演示信号
- 不改 Momentum/Tide/Prism 参数与因子权重；不调 Champion 门槛；不为改善收益/Sharpe 重跑优化
- 不做大规模重构；不把"测试通过"等同于"问题不存在"
- RQAlpha 不得提前启动，不得跳过数据对账直接做策略对账

## 七、验证命令速查

```bash
cd web && npm test                       # 158 tests, node --test + tsx
cd web && ./node_modules/.bin/tsc --noEmit
cd research && uv run pytest -q          # 260 tests
cd research && uv run python -m ashare_research.cli audit-universe-coverage \
  --output runtime/universe-coverage.json   # 阻塞2 覆盖审计，严重缺失 exit 1
```
