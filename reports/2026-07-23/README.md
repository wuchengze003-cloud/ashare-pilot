# 2026-07-23 全项目检查与优化 · 总报告

本轮完成：全项目四路检查（web / pyserver / research / 安全运维）、Tushare 与 Kimi 插件双路数据源实测、近一月宏观调研、股票池重选方案、策略进化管线推进、LLM/数据源适配层实施。

## 产出文件索引

| 文件 | 内容 |
|---|---|
| [web-check.md](./web-check.md) | web 检查：修复 6 处 + 补 11 测试，89/89 → 106/106 全绿，tsc 零错误 |
| [pyserver-check.md](./pyserver-check.md) | pyserver 检查：修复 6 处（WAL、并发锁库、缓存容错等），接口实测 |
| [research-check.md](./research-check.md) | research 检查：pytest 54 全绿；Tushare 断供、注册表未初始化等发现 |
| [security-ops-audit.md](./security-ops-audit.md) | 安全运维审计：无高风险；monitoring/tmp 未忽略等 3 个中危 |
| [tushare-benchmark.md](./tushare-benchmark.md) | Tushare 接口实测：tenant key 过期，整体不可用 |
| [pool-performance-1m.md](./pool-performance-1m.md) / [.json](./pool-performance-1m.json) | 123 只近一月行情指标 + 19 主题强弱 + 池外候选 |
| [kimi-datasource-benchmark.md](./kimi-datasource-benchmark.md) | iFinD/Wind/Gildata 三插件 12 次调用实测 + 可移植性验证 |
| [macro-brief.md](./macro-brief.md) | 2026-06-23~07-23 宏观与 19 主题产业事件（双源交叉验证） |
| [pool-rebalance-proposal.md](./pool-rebalance-proposal.md) | **股票池重选方案：45 条调整，待你确认后落盘** |
| [strategy-evolution-run.md](./strategy-evolution-run.md) | 策略进化推进记录 + Codex 验收要点 |
| [datasource-decision.md](./datasource-decision.md) | 数据源决策：Tushare 恢复兜底 + Kimi 桥接增量，P0-P4 路线 |
| [data/](./data/) | Kimi 插件落盘原始证据（19 个文件） |

## 三大关键结论

1. **Tushare 断供是当前唯一 P0 阻断**。第三方代理（tu.brze.top）tenant key 于 07-16 过期，官方端点同 token 校验失败（40101）。web 行情靠东财/腾讯 fallback 续命，但 research 晋级管线（特征面板硬依赖 adj_factor / stk_limit）无替代源，fail-closed 停摆。已验证：停摆是安全的（零副作用、无静默训练路径），账本已初始化（ledger-init 幂等通过），管线推进到"只差数据恢复"就绪状态。
2. **Kimi 插件实测可用但不可整体替换 Tushare**。iFinD 日线 0.33s、Wind 1.48s（字段与 Tushare 对齐且数值交叉一致）、Gildata 为唯一可用选股源；但受 3 标的/次、3 年/次、无缓存、KIMI_API_KEY 仅 Kimi Work 进程注入四重约束。决策：**Tushare 官方 token 恢复兜底 + 「Kimi 定时落盘 → 项目只读」桥接做增量**（选股/一致预期等 Tushare 没有的能力），项目代码零凭据，Codex 等任何环境可用。
3. **近一月市场剧烈分化，股票池需结构性调整**。沪深300 -3.89%、创业板指 -14.71%；池内仅数据中心网络（+49.76%)、AI服务器、晶圆代工、云/AI基建、电力收涨，光模块（-33.76%)、AI-PCB、存储领跌。宏观面与行情面背离：光模块/存储基本面利好强度 5（订单饱满、DRAM 涨价 53~63%）但高位拥挤暴跌。方案按"景气 E × 行情 P × 验证 V"三维规则处理：龙头留 core（建议打分器侧加 crowded_drawdown 标签），边缘股降级剔除。

## 代码改动清单（未提交，git status 可见）

- **web（A1 检查修复）**：新增 backtestConfig.ts 请求校验（400 替代 500）、修复 dashboardData 时区混用（威胁 point-in-time 防线）、mapPool limit 边界、signals asOf 校验、refresh 令牌恒定时间比较；+11 测试
- **web（B3 适配层）**：新增 `lib/llm/` provider 抽象（LLM_PROVIDER 可切换 DeepSeek/Kimi，deepseek.ts 改为兼容垫片、调用方零改动）、新增 `lib/kimiBridge.ts`（零凭据桥：schema 校验 + 48h 新鲜度拒用 + 三态优雅降级）；+17 测试，合计 106/106 全绿
- **pyserver（A2 检查修复）**：SQLite 改 WAL + busy_timeout 10s、损坏缓存容错、/klines 日期校验 400、缓存 key 大小写归一、HK spot 排序、周末 TTL；9 请求实测
- **research（B2）**：仅 ledger-init 初始化空账本（runtime 在 gitignore 内，无 tracked 文件改动）；注册表/active_model 未触碰
- **package.json**：overrides 安全 pin（js-yaml 4.3.0、sharp 0.35.3）

## 待你决策清单（按优先级）

| # | 决策项 | 说明 | 建议 |
|---|---|---|---|
| P0 | **Tushare 密钥恢复** | 代理续费 or 换官方 token（官方 5000 积分档可覆盖现有接口），恢复后需回填 2018 至今约 2000+ 交易日 | 官方 token 更稳，摆脱代理单点 |
| P1 | **股票池调整 45 条** | 见 pool-rebalance-proposal.md：剔除 15、降级 20、升级 1、新入 watch 9；池 123 → 117（85c+32w） | 确认后我落盘 universe.json（保留点时生效字段） |
| P2 | **Kimi 桥接写入侧** | 是否创建 Kimi 定时任务，定期把 Gildata 选股 / 一致预期落盘到 data/kimi-bridge/ | 建议建，配合 P0 恢复后双线并行 |
| P3 | monitoring/ tmp/ 处理 | 含个人持仓与飞书群 ID，未跟踪未忽略，误 git add -A 即泄露 | 加入 .gitignore 或移出仓库根 |
| P4 | Node 版本策略 | Node 24 下 better-sqlite3 编译失败，需 Node 22 或升级 v12 | package.json 加 engines 约束 |
| P5 | start.sh 强杀端口 | 会强杀任意占用 8001/3000 的进程，与注释不符 | 改为只杀自身 PID 或提示 |

## 数据 caveat

本轮行情与候选数据来自东财/腾讯 fallback（Tushare 断供），窗口截至 07-22；候选股为行情初筛未经基本面复核。Tushare 恢复后应重跑 pool-performance-1m 校验阈值。
