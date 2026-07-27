# Kimi 内置金融数据插件实测与可移植性验证报告

- 实测日期：2026-07-23（A 股交易日，盘中/盘后调用均已覆盖）
- 实测人：Kimi数据源_实测员_A6（子代理）
- 环境：Kimi Work 托管 Python 3.12，agent-gw SDK 0.2.6（实测时按插件技能说明从官方 CDN 安装）
- 样本标的：300308.SZ 中际旭创、688256.SH 寒武纪（均为项目 AI 算力产业链核心持仓候选）
- 原始证据：`reports/2026-07-23/data/` 下各 `*_log.txt` 与返回 CSV；iFinD 完整 API 文档存档于 `reports/2026-07-23/ifind-describe.md`

---

## 1. 三插件逐项实测记录

### 1.1 iFinD（同花顺）—— 独立插件，9 个 API

`describe` 返回文档宣称覆盖"智能选股"，但实际 API 清单只有 9 个，**无选股接口**（实测 `ifind_intelligent_stock_screening` 返回 `API_NOT_FOUND`，服务端列出的可用清单证实只有 9 个）。

| # | 测试项 | API / 参数 | 结果 | 耗时 | 字段完整度 | 返回格式 |
|---|--------|-----------|------|------|-----------|---------|
| 1 | 近一月日线（前复权） | `ifind_get_price`，300308.SZ+688256.SH，2026-06-23~07-23 | ✅ | 0.33s | OHLC+volume+代码/日期/中英文名/币种，**缺成交额(amount)、涨跌幅、换手率** | JSON 包裹 CSV 预览 + 落盘 CSV，44 行×2 标的 |
| 2 | 收盘摘要（当日） | `ifind_get_stock_realtime_price` type=close_summary | ✅ | 0.75s | pre_close/OHLC/vwap/chg/pct_chg/volume/**amt**/turn，13 字段最全 | 同上，15:29 已能取到当日收盘 |
| 3 | 财务报表（三表） | `ifind_get_financial_statements` statement=all，2026Q1 | ✅（有缺陷） | 0.41s | 字段为 `ths_*_stock` 内部代码（现金流量表 112 列），**无中文/英文可读字段名**，需自行映射；**三表写入同一 file_path 互相覆盖，只存活最后一张** | CSV 落盘 |
| 4 | 盈利预测 | `ifind_get_forecast` | ✅ | 0.33s | FY1-FY3 净利/营收预测+YoY+PE/PEG/PB/PS，字段同样为内部代码 | CSV |
| 5 | 智能选股 | 猜测名调用 | ❌ | 0.32s | `API_NOT_FOUND`，确认该数据源无此能力 | 结构化错误信息（良好） |
| 6 | 三年日线（720 行） | `ifind_get_price`，2023-08-01 起不复权 | ✅ | 0.41s | 同 #1；>3 年（1148 天）返回 `PARAMETER_ERROR`（上限 1095 天） | CSV |

**其他约束**：单次最多 3 个 ticker；`realtime_tech` 不支持 688 开头科创板（寒武纪排除在外）；行情错误提示清晰（错误域、可读消息）。

### 1.2 Wind（万得）—— xtt 套件内置技能，35 个 API

调用通道：`datasource-wind/scripts/wind_tool.py`（agent-gw 同源），另有捆绑的 `wind-mcp` Node CLI。

| # | 测试项 | API / 参数 | 结果 | 耗时 | 字段完整度 | 返回格式 |
|---|--------|-----------|------|------|-----------|---------|
| 1 | 近一月日线（前复权） | `wind_get_price`，300308.SZ+688256.SH，2026-06-23~07-23 | ✅ | 1.48s | trade_date/wind_code/**OHLC+volume+amt(成交额)**，与 Tushare `daily` 字段几乎一一对应 | JSON 预览 + 落盘 CSV（file_path 需绝对路径） |
| 2 | API 目录 | `wind_tool.py describe` | ✅ | 0.37s | 35 个 API：股/基/指/债/公告/新闻/宏观 EDB/财务/技术指标 | Markdown |

**交叉验证**：Wind 与 iFinD 同日 OHLC 前复权数值完全一致（如 300308 于 2026-06-23 收 1310.01），两家数据同源可信。Wind 未在 describe 中发现独立"资金流"API（技能说明称资金流走 stock NL 工具且需运行时确认），本次未深入。

### 1.3 Gildata（恒生聚源）—— xtt 套件内置技能，8 个 API

| # | 测试项 | API / 参数 | 结果 | 耗时 | 字段完整度 | 返回格式 |
|---|--------|-----------|------|------|-----------|---------|
| 1 | 自然语言智能选股 | `gildata_smart_stock_selection`："光模块或CPO概念、总市值>500亿、近20日涨幅为正" | ✅ | **10.8s** | 筛出 12 只；含总市值多日序列、区间涨跌幅(前复权)、概念名称、收盘价等 | **Markdown 表格嵌在 CSV 单元格内**，机器解析需二次处理 |
| 2 | 自然语言行情查询 | `gildata_fin_query`：中际旭创 7-22 行情 | ✅ | **14.5s** | 返回 31 字段实时快照（含买卖盘、涨跌停价、总市值/流通市值、换手率、量比）；NL 理解偏差：要 7-22 历史，给了 7-23 最新 | 同上 Markdown-in-CSV |

Gildata 是三家中**唯一可用智能选股**的数据源；技能明示其为"数据库级 EOD，无盘中 tick/分钟线"，且**不覆盖龙虎榜/主力资金/融资融券**（指引回 iFinD）。

---

## 2. 与 Tushare Pro 可比项对比表

项目 pyserver 当前用法：A 股 K 线首选 easy-tdx（通达信 TCP，无 token 无配额），Tushare 兜底 `daily` 及 `daily_basic`/`fina_indicator`/`report_rc`/`stock_basic`，港股走 akshare（因 Tushare `hk_daily` 免费层 2 次/分、10 次/日硬上限——见 pyserver/main.py 注释）。

| 维度 | Tushare Pro（现状） | iFinD 插件 | Wind 插件 | Gildata 插件 |
|------|--------------------|-----------|-----------|--------------|
| A 股日线字段 | OHLCV+amount+pct_chg 等，英文可读名 | OHLCV，**缺 amount** | **OHLCV+amt，与 Tushare 对齐** | 快照类丰富，历史序列需 NL 逐次查 |
| 近一月日线耗时 | 本地 SQLite 命中≈0；远端约 0.5-2s（含限流） | **0.33s** | 1.48s | 14.5s（NL 解析开销） |
| 单次批量 | 单标的（按 ts_code 循环） | ≤3 标的 | 多标的逗号分隔 | NL 语义，不适合批量 |
| 历史深度 | 全历史 | ≤3 年/次 | 未明示（文档未限） | 不适合大序列 |
| 财务报表 | `fina_indicator` 等，字段英文可读 | 三表全但字段为 `ths_*` 内部码，需映射层；all 模式覆盖同一路径（缺陷） | 有独立 `wind_get_financial_data`/`financial_index`（未逐项实测） | 财务强（聚源 JYDB），NL 查询 |
| 智能选股 | 无 | **宣称有，实际无** | 有 `wind_search_stocks`（未实测） | **唯一实测可用**（10.8s/次） |
| 资金流/龙虎榜 | `moneyflow` 等有积分门槛 | 技能指引称覆盖 A 股微结构（本次未实测） | 需运行时确认 | **明确不支持** |
| 盘中/实时 | 无（项目靠 easy-tdx） | close_summary/open_summary/分钟 K 齐全 | `wind_get_stock_quote` 分钟级（未实测） | 无盘中 |
| 返回格式 | DataFrame（代码直用） | JSON+CSV 落盘，字段内部码 | JSON+CSV 落盘，字段规整 | Markdown-in-CSV，需解析 |
| 成本 | 积分制（日线级约 2000 积分/年档，付费；此为印象值，未当轮核实） | Kimi 订阅内含，无显式配额（限流策略未公开） | 同左 | 同左 |
| 认证依赖 | `TUSHARE_TOKEN`（pyserver/.env 自有） | **KIMI_API_KEY / agent-gw 网关** | 同左 | 同左 |

---

## 3. 可移植性验证（关键结论）

### 3.1 认证链实测（仅验证存在性，未读取任何密钥内容）

- agent-gw SDK 0.2.6 源码确认的凭据解析顺序：**显式 `api_key=` 参数 > `KIMI_API_KEY` 环境变量 > `~/.kimi/agent-gw.json`（`{"api_key","base_url","kimi_chat_id"?}`）> 抛错**。网关地址默认 `https://agent-gw-dev.dev.kimi.team/coding`，可被 `KIMI_BASE_URL`/配置覆盖；传输为 HTTPS + `Authorization: Bearer`，可选 `X-Kimi-Chat-Id` 头绑定会话配额。
- 本机现状：`KIMI_API_KEY` 存在（长度 72）且 `KIMI_BASE_URL` 已设，但**二者均不在 ~/.zshrc 等 shell 配置中——只由 Kimi Work 运行时注入其托管进程**；`~/.kimi/` 目录不存在（无 agent-gw.json）。

### 3.2 结论：非 Kimi Work 环境能否直接复用？

**技术上可以，工程上有条件、有风险。**

- ✅ 有利条件：SDK 为纯 Python（仅依赖 requests），从公网 CDN manifest  pip 安装即可；认证只是一个 Bearer key + 一个 HTTPS 网关，无本地服务、无 Kimi Work 进程依赖。Codex CLI / CI 只要 `pip install agent-gw` + 导出 `KIMI_API_KEY`（必要时 `KIMI_BASE_URL`）+ 可访问 `*.kimi.team`，即可原样调用 `ifind_tool.py`/`wind_tool.py`/`gildata_tool.py`。
- ⚠️ 风险与不确定性：
  1. **Key 分发**：key 当前只活在 Kimi Work 进程环境里，导出到 CI/Codex 需人工拷贝，属于把 Kimi 账户凭据扩散到第三方环境，违背"密钥最小暴露"原则，且 key 的轮换/吊销策略未公开；
  2. **配额与授权模型不透明**：三插件是否绑定 Kimi Work 会话（`X-Kimi-Chat-Id`）、有无日配额、超额行为如何，官方文档未说明；把生产数据链路押在未公开 SLA 的网关上风险高；
  3. **base_url 为 dev 域名**（`agent-gw-dev.dev.kimi.team`），稳定性/长期可用性未知；
  4. Tushare 则相反：token 自有、配额明示、可脱离任何 agent 运行。

### 3.3 推荐隔离方案：Kimi 定时落盘，项目只读文件

**首选（低耦合、零凭据扩散）**：用 Kimi Work 的 Automation 定时任务，在每个交易日 15:35 后由 Kimi 环境内的 agent 调用上述插件，把结果以 CSV/Parquet 落盘到项目约定目录（如 `data/kimi_feed/YYYYMMDD/`），pyserver 增加一个 file-based loader 与现有 `tushare_daily` 并列作为数据源。项目代码零 agent-gw 依赖、零 KIMI_API_KEY 暴露；Kimi 侧故障时自动回落 Tushare/easy-tdx。

**次选（需要 Kimi 独有能力时）**：仅在"智能选股/研报/一致预期"等 Tushare 没有的能力上，由 Kimi 侧按需调用 Gildata/iFinD 并落盘，而非全量替换。

**不建议**：在 pyserver/web 生产运行时内嵌 agent-gw SDK 直连——认证链、配额、dev 域名三项不确定性都直接传导到生产链路。

---

## 4. 集成建议（针对替换 Tushare+DeepSeek 的决策）

1. **不要整体替换**。Kimi 插件在"近一月日线"这一核心场景上，iFinD 缺 amount 字段、Wind 字段对齐但两者都受 3 年/次、批量≤3 标的限制，且认证依赖 Kimi 账户；Tushare+easy-tdx 在批量历史 K 线上仍更稳。
2. **分层采用**：
   - 行情主干：维持 easy-tdx 首选 + Tushare 兜底 + SQLite 缓存的现状；
   - **增量增强**：Gildata 智能选股可替代 DeepSeek 做"股票池维护"的自然语言筛选（实测 12 只光模块/CPO 标的可用），但需处理 Markdown-in-CSV 解析与 10-15s 延迟，只适合做日级离线任务，不适合盘中；
   - 一致预期/研报：iFinD `forecast`、Gildata 研报/一致预期是 Tushare 免费层没有的增量，值得通过落盘方案引入 research/ 因子库；
   - 财报：iFinD 三表字段需先做 `ths_*` → 可读名映射层，且规避 all 模式文件覆盖缺陷（分三次调用各给独立 file_path）。
3. **上线前待办**：实测 Wind 资金流/分钟线、确认 agent-gw 配额模型与 key 生命周期、落盘目录约定与 pyserver loader 接口设计。

---

## 5. 本次未覆盖/遗留风险

- Wind 的 34 个其余 API、Gildata 宏观/基金/公告、iFinD 公告/股东/技术指标未逐项实测；
- Tushare 积分档位价格为印象值，未当轮联网核实；
- agent-gw 网关的限流、配额、key 有效期无法从客户端观测，需官方文档或实测压测确认；
- 三插件均无本地缓存层，重复取数全部走网关，大批量回测取数（数千标的×多年）未做压测，预计受 3 标的/次与未知限流双重约束。
