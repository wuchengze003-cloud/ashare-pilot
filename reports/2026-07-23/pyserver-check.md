# pyserver/ 检查报告

- 检查人：Pyserver_检查员_A2
- 日期：2026-07-23
- 范围：`pyserver/main.py`（1315 行）、`pyproject.toml`、`cache.db`；未触碰 `.env`、未打印任何密钥
- 方法：`uv sync`（58 包锁定一致）→ 通读源码 → 纯函数/缓存单测 → 起 uvicorn（端口 8013）实测 9 个请求 → 关闭并确认端口释放

## 架构概览

FastAPI 侧车，数据源分层清晰：A 股 K 线/实时行情优先 easy-tdx（通达信 TCP，无配额），依次回退 Eastmoney push2 → 腾讯 → 新浪 → Tushare；HK 走 akshare（避开 Tushare hk_daily 10 次/日限制）。SQLite 单表 KV 缓存（8594 条），K 线/基本面按交易日 TTL，spot 30s TTL。接口契约用 Pydantic 模型（Kline/Fundamental/Analyst），目标价有清洗门控（>+200% upside 或 >10000 元视为坏数据）。整体设计质量好：重试、令牌桶限速、TDX 断线退避、缓存未命中标记、`_source_status` 可观测性都到位。

## 问题清单与已修复项

### 已修复（6 项，均为低风险）

| # | 严重度 | 位置 | 问题 | 修复 |
|---|--------|------|------|------|
| 1 | 中 | `db()` / 启动引导 | SQLite 为 rollback journal（`journal_mode=delete`）且无 busy_timeout；`/spots` 线程池并发写下读写互斥，高并发时可能 `database is locked` | 启动时置 `journal_mode=WAL`；每连接 `timeout=10` + `PRAGMA busy_timeout=10000` |
| 2 | 低 | `cache_get` | 缓存行 JSON 损坏时 `json.loads` 抛异常，命中该 key 的请求全部 500 | 捕获 `JSONDecodeError`，删除坏行并按未命中处理 |
| 3 | 低 | `seconds_until_next_trading_close` | 周五收盘后 TTL 指向周六 15:30，周末过期会触发一次结果完全相同的上游重抓 | 目标时间落在周六/周日则顺延到周一 |
| 4 | 低 | `/klines` | 日期参数无校验，非法日期（如 `2026/07/01`）直达上游变 502；`start > end` 无拦截 | 新增 `_checked_date`：非法格式返回 400；`start > end` 返回 400 |
| 5 | 低 | `/klines` `/spot` `/fundamental` `/spots` | 缓存 key 用原始 symbol，`SH600519` 与 `sh600519` 各存一份重复缓存 | 新增 `_norm_symbol`（strip+lower）统一 key；响应中 symbol 仍回显原始入参 |
| 6 | 低 | `/spot` HK 分支 | A 股分支 `sort_values("trade_date")` 后取末行，HK 分支直接 `iloc[-1]` 依赖上游行序 | HK 分支重命名后同样按 `trade_date` 排序 |

修复后单测：符号归一化、日期校验（400）、缓存读写、坏行容错、WAL 模式全部通过。

### 遗留问题 / 建议（未修复）

| # | 严重度 | 位置 | 说明 |
|---|--------|------|------|
| A | 中（需核实） | `_tdx_spot` / `_sina_spot` / `_tencent_spot` / tushare fallback | spot 各数据源 `volume` 单位可能不一致（K 线已显式把 TDX 股数 ÷100 归一化为手，spot 路径无类似归一化；新浪 volume 为股数）。跨源比较成交量前需逐源核实单位，不属于低风险改动，未动 |
| B | 低 | `cache_update_keep_age` | 定义后全项目无调用，属死代码，可删或留作他用 |
| C | 低 | `spots()` | 修复 #5 后，同一请求里大小写变体（`sh600519,SH600519`）会命中同一缓存行并按行内 symbol 去重，大写条目被折叠丢弃；前端统一小写，无实际影响 |
| D | 低 | `_patch_tushare_http_url` | 依赖 tushare 内部属性名 monkey-patch，升级 tushare 可能失效；已有失败即报错兜底，属有意设计 |
| E | 外观 | `pyproject.toml` version 0.1.0 vs `FastAPI(version="0.2.0")` 不一致；`threading`/`concurrent.futures` import 位于文件中段 | 风格问题，不影响运行 |
| F | 环境 | `/spot?symbol=hk00700` | 实测 502（akshare `stock_hk_hist` 连接被远端断开）。错误处理、`_mark_source` 标记、502 语义均正确，属外部网络/上游问题，需在网络正常环境复测 HK 链路 |

## 接口实测结果（uvicorn :8013，测后已关闭并确认端口释放）

| 请求 | 结果 | 耗时 | 备注 |
|------|------|------|------|
| `GET /health` | 200 | 1ms | 返回源优先级与 sources 快照 |
| `GET /klines?symbol=sh600519&start=20260701` | 200 | <1s（含缓存命中） | qfq 日线自 2026-07-01 起连续完整，volume 单位为手 |
| `GET /klines?...&start=2026/07/01` | **400** | 2ms | 修复 #4 生效：`invalid start date` |
| `GET /klines?...start=20260720&end=20260701` | **400** | 1ms | 修复 #4 生效：`start is after end` |
| `GET /spot?symbol=sh600519` | 200 | 16ms | easy_tdx 源，价 1292.01，-1.0% |
| `GET /spots?symbols=sh600519,sz000858,SH600519` | 200 | 24ms | 去重后 2 行，均 easy_tdx 实时价 |
| `GET /fundamental?symbol=sh600519` | 200 | 4.3s | pe_ttm 19.62 / pb 6.85 / 市值 16151 亿（Eastmoney 源） |
| `GET /analyst?symbol=sh600519` | 200 | 31ms | 模型规则目标 1354.93，置信度 0.743，upside 4.87%，来源标注 `model_atr_momentum_v1` |
| `GET /spot?symbol=hk00700` | 502 | 4.0s | 上游 akshare 连接被断开（见遗留 F），非代码缺陷 |

## 副作用说明

- `cache.db` 已切换 WAL 模式，目录下会出现 `cache.db-wal` / `cache.db-shm` 伴生文件，属正常现象；两文件已在 `.gitignore` 语义内（cache.db 本身即生成物，不应入库）。
- 测试服务（8013）已关闭，`lsof` 确认端口释放；用户自有的 8001 端口服务未动。
