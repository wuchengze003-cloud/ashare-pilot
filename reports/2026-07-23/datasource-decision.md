# 数据源与 LLM 适配层决策报告（Tushare vs Kimi 插件）

- 日期：2026-07-23
- 作者：数据源适配_架构师_B3（子代理）
- 输入：`tushare-benchmark.md`（Tushare 断供实测）、`kimi-datasource-benchmark.md`（iFinD/Wind/Gildata 实测 + 可移植性结论）、`data/` 下落盘样例
- 配套代码：本轮已落地 LLM provider 抽象层（`web/lib/llm/`）与 Kimi 数据桥（`web/lib/kimiBridge.ts`），见文末 §5

---

## 1. 现状一句话

Tushare 代理 tenant key 已于 2026-07-16 过期、同一 token 在官方端点校验失败，**Tushare 数据链路整体断供**；Kimi 内置金融插件实测可用，但认证链、配额、dev 域名三项不透明，**不适合直接嵌入生产运行时**。决策核心不是"二选一"，而是"分层共存、故障隔离"。

## 2. Tushare vs Kimi 插件对比结论（基于两份实测）

| 维度 | Tushare Pro | Kimi 插件（iFinD/Wind/Gildata） |
|------|------------|-------------------------------|
| 当前状态 | ❌ 断供（代理 key 过期 + 官方端点 40101） | ✅ 实测可用（Wind/iFinD 日线交叉验证一致） |
| 认证模型 | 自有 token，配额明示，可脱离任何 agent | `KIMI_API_KEY` 只注入 Kimi Work 进程，导出即凭据扩散 |
| 批量历史 K 线 | 全历史、按 ts_code 循环、SQLite 缓存 | ≤3 标的/次、iFinD ≤3 年/次、无本地缓存 |
| 字段对齐 | 英文可读字段，含 amount/pct_chg | Wind amt 与 Tushare 对齐；iFinD 缺 amount |
| 独有能力 | 无智能选股；免费层缺一致预期 | **Gildata 智能选股唯一实测可用**；iFinD 一致预期是增量 |
| 延迟 | 本地缓存≈0，远端 0.5-2s | 0.3-1.5s（Gildata NL 选股 10-15s，仅适合离线） |
| 故障模式 | 显式报错（tenant key expired / 40101） | 配额/限流/key 生命周期均不公开，不可观测 |

结论：**Kimi 插件是优秀的"增量数据源"和现成的"断供期备份"，但不是合格的"行情主干替换品"**——批量约束（3 标的/次）、历史深度约束（3 年/次）、无缓存层、认证不可移植，任何一条都足以否决整体替换。

## 3. 决策建议：Tushare 续期 vs 代理 vs Kimi 桥接

### 3.1 三条路线的取舍

| 路线 | 成本 | 风险 | 结论 |
|------|------|------|------|
| **A. 维持第三方代理、等续期** | 零开发 | 单点依赖代理运营方；本次已实测"过期即全断"且无 SLA；恢复时间不可控 | 短期可等，**不可作为唯一依赖** |
| **B. 换 Tushare 官方原生 token** | 付费积分（daily_basic/moneyflow/fina_indicator 约需 2000+ 积分档，金额为印象值未当轮核实） | 低：配额明示、官方 SLA、可离线运行 | **推荐作为行情主干兜底** |
| **C. Kimi 桥接（定时落盘 → 项目只读）** | 一次开发（本轮已完成读取侧）+ 一个 Kimi Work 定时任务 | 依赖 Kimi Work 环境在线；落盘任务失败表现为"数据过期"而非报错，需新鲜度监控 | **推荐作为增量数据源与断供期备份** |

### 3.2 最终决策

1. **行情主干不动**：easy-tdx 首选 + SQLite 缓存的现状维持；Tushare 兜底位按路线 B 恢复（申请官方原生 token），代理仅作过渡，恢复后弃用代理 key。
2. **Kimi 走桥接不进运行时**（路线 C）：严禁在 pyserver/web 内嵌 agent-gw SDK 直连；所有 Kimi 数据经"Kimi Work Automation 盘后落盘 → `data/kimi-bridge/` → 项目只读"单向流动。本轮已落地读取侧 `web/lib/kimiBridge.ts`（schema 校验 + 48h 新鲜度拒用 + 缺失优雅降级）。
3. **LLM 层可切换**：DeepSeek 仍为默认；`LLM_PROVIDER=kimi` 可切到 Moonshot OpenAI 兼容端点或本地桥接，缺 key 时报 `LlmUnavailableError`（配置错误，语义上不可重试），与请求级错误区分。本轮已落地 `web/lib/llm/`，调用方（universe-refresh 等）零改动。
4. **增量能力按需引入**：Gildata 智能选股（替代 DeepSeek 做股票池初筛，需处理 Markdown-in-CSV 与 10-15s 延迟，仅限日级离线）、iFinD 一致预期（入 research/ 因子库）通过桥接 feed `screen-results` / `consensus` 落地，字段契约 v1 已定义在 `kimiBridge.ts` 注释中。

## 4. 分阶段迁移路线

- **P0（已完成，本轮）**：LLM provider 抽象 + Kimi 桥读取侧 + 17 个新测试 + env 文档。任何环境拿到桥接目录即可无凭据消费 Kimi 数据。
- **P1（断供恢复，1 周内）**：申请 Tushare 官方原生 token 并配置到 pyserver/.env；重跑 `tushare-benchmark.md` 实测脚本校验；在 pyserver 健康检查中显式上报代理/token 错误（消除静默降级）。恢复前由 easy-tdx 独立支撑行情。
- **P2（桥接写入侧，2 周内）**：创建 Kimi Work 定时任务，交易日 15:35 后落盘 `screen-results`（Gildata 选股）与 `consensus`（iFinD 一致预期）CSV 至 `data/kimi-bridge/`；`daily:close` 流程读取桥接状态并记录 `bridgeStatus()`。
- **P3（能力整合，1 月内）**：股票池维护工作流改为"Gildata 初筛（桥接）+ LLM 复审（provider 层）"两段式；iFinD 一致预期入 research/ 因子库前先做 `ths_*` 字段映射层；iFinD 财报分三次独立 file_path 调用规避 all 模式覆盖缺陷。
- **P4（验收，2026 春节后窗口）**：按 AGENTS.md 约定，2026 post-CNY 窗口仅做最终验收，不做参数选择；桥接数据质量（覆盖率、字段漂移）在该窗口出验收报告。

## 5. 本轮代码改动清单

| 文件 | 改动 |
|------|------|
| `web/lib/llm/types.ts` | 新增：ChatMessage/ChatOptions/LlmProvider 契约、`LlmUnavailableError` |
| `web/lib/llm/openaiCompat.ts` | 新增：OpenAI 兼容 /chat/completions 客户端（超时/中止/json_object 预校验，防毒缓存逻辑原样保留） |
| `web/lib/llm/providers.ts` | 新增：deepseek（默认）+ kimi（预留）provider，env 惰性解析，未知 provider 快速失败 |
| `web/lib/llm/index.ts` | 新增：`chat()` 入口，缓存键含 provider 名防跨模型串缓存；缺 key 检查先于缓存命中（与原语义一致） |
| `web/lib/deepseek.ts` | 改为兼容垫片，re-export `chat`；`universe-refresh.ts` 等调用方零改动 |
| `web/lib/kimiBridge.ts` | 新增：桥目录解析、`screen-results`/`consensus` 两个 feed、CSV 解析器（支持引号内换行/逗号，兼容 Gildata Markdown 单元格）、mtime 新鲜度、缺失/过期/非法三态降级、`bridgeStatus()` 诊断 |
| `web/test/llm-provider.test.ts` | 新增 6 例：provider 切换、缺 key 降级、未知 provider、json_object 校验 |
| `web/test/kimi-bridge.test.ts` | 新增 11 例：缺失降级、schema 校验、新鲜度拒用、日期文件选择、CSV 解析边界 |
| `web/env.example.txt` | 新增 `LLM_PROVIDER`/`LLM_TIMEOUT_MS`/`KIMI_LLM_*`/`KIMI_BRIDGE_*` 说明 |

验证：Node 22.22.0 下 `npm test` 106/106 通过（含旧 deepseek 3 例回归），`tsc --noEmit` 无错误。Node 24 下 better-sqlite3 编译失败，须使用 Node 22。

## 6. 遗留风险

1. Tushare 官方积分档位与价格为印象值，P1 申请前需当轮核实；
2. 桥接写入侧（Kimi Work 定时任务）尚未创建，P2 前 `data/kimi-bridge/` 为空目录属预期行为；
3. agent-gw 配额/key 生命周期不公开，桥接方案已规避，但若未来需要 Kimi 实时能力仍需官方文档确认；
4. Gildata 返回 Markdown-in-CSV，写入侧落盘前需在 Kimi 侧完成表格 → 规整行的转换（契约 v1 已按规整行设计）；
5. Wind 资金流/分钟线、iFinD 公告/股东等未实测 API，P3 引入前需补测。
