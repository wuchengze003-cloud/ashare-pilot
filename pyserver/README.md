# pyserver —— easy-tdx + Tushare + AkShare sidecar

基于 FastAPI 的轻量 sidecar，封装 easy-tdx、[Tushare Pro](https://tushare.pro) 与 AkShare，只对外暴露 Next.js 网站需要的端点。

所有响应都写入 `cache.db`（SQLite），按端点设置分层 TTL：

| 端点 | TTL | 数据源 |
|---|---|---|
| `GET /klines` | 直到下一个 15:30 A 股收盘 | A 股优先 easy-tdx 通达信日 K，覆盖不足回退 `ts.pro_bar(adj='qfq')`；港股 `ak.stock_hk_hist` |
| `GET /fundamental` | 30 秒到 24 小时 | A 股优先 AkShare 东方财富全市场快照；缺字段回退 `pro.daily_basic` |
| `GET /analyst` | 由底层 spot/K 线缓存决定 | 实时价 + 15-30 日 ATR/动量/前高规则测算目标价；不请求研报、新闻或 LLM |
| `GET /analysts` | 由底层 spot/K 线缓存决定 | 批量包装 `GET /analyst`，避免前端逐行请求 |
| `GET /spot` | 30 秒 | A 股优先 easy-tdx 通达信实时行情，缺失依次回退 Eastmoney/Tencent/Sina/Tushare daily；港股 `ak.stock_hk_hist` |
| `GET /spots` | 30 秒 | 批量包装 `GET /spot`，优先读取 `cache.db`，避免前端逐行请求 |

## Token

需要 [Tushare Pro 账号](https://tushare.pro/register)。把 token 放进 `pyserver/.env`（已 gitignore）：

```
TUSHARE_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

启动时通过 `python-dotenv` 自动加载。

## 运行

依赖通过 [uv](https://docs.astral.sh/uv/) 管理 —— `pyproject.toml` 为依赖清单，`uv.lock` 锁定精确版本。

```bash
uv sync                                      # 创建 .venv 并安装锁定的依赖
uv run uvicorn main:app --port 8001 --reload
```

新增/升级依赖：

```bash
uv add <pkg>           # 写入 pyproject.toml + uv.lock
uv lock --upgrade      # 整体升级
```

## 为什么用 sidecar？

Tushare 仅有 Python SDK，AkShare 也主要在 Python 生态使用。把它们放进一个独立的 FastAPI 进程，可以让 Next.js 端保持纯 TypeScript，同时通过稳定、强类型、自带缓存的 HTTP 接口消费它。sidecar 还集中处理：

- 符号格式归一化（`688256` ↔ `688256.SH`，`hk00700` ↔ `00700.HK`）。
- 退避重试（3 次指数退避），吸收 Tushare 偶发抖动。
- HK 接口的 token-bucket 限速（`pro.hk_daily` 免费档 2 次/分钟）。
- A 股 easy-tdx 通达信行情优先，用于实时现价和日 K；Eastmoney/Tencent/Sina 免费行情作为现价兜底；AkShare 东方财富快照用于 PE/PB/市值等补充字段。
- 目标价为短线交易目标：现价 + max(ATR14 倍数、前高突破空间、动量项)，上行 18% 封顶；没有建设性形态时返回 `implied_target: null`。不再用 EPS×PE，也不读研报/新闻。
- `/spot` 响应包含 `source/as_of`；`/health` 返回最近各数据源状态和优先级，方便定位偏差来自哪个通道。
- 名称缓存（`stock_basic` / `hk_basic` 进程内 LRU）。

## 端点速查

```bash
# 健康检查
curl http://localhost:8001/health

# 日 K（前复权）
curl 'http://localhost:8001/klines?symbol=688256&start=20240101'

# 基本面（PE/PB/市值，24h 缓存）
curl 'http://localhost:8001/fundamental?symbol=300476'

# 规则目标价（实时价 + 日 K 计算）
curl 'http://localhost:8001/analyst?symbol=300476'

# 批量规则目标价，前端股票池表格使用这个接口
curl 'http://localhost:8001/analysts?symbols=300476,601138,688256'

# 最新价/最近收盘（30 秒缓存）
curl 'http://localhost:8001/spot?symbol=hk00700'

# 批量最新价/最近收盘，前端股票池表格使用这个接口
curl 'http://localhost:8001/spots?symbols=300476,601138,688256'
```

## 代码符号规则

所有端点接受同一套符号写法（与 ts_code 自动互转）：

| 市场 | 输入 | 内部 ts_code |
|---|---|---|
| 沪市 A 股 | `sh600519` 或 `600519` | `600519.SH` |
| 深市 A 股 | `sz000858` 或 `000858` | `000858.SZ` |
| 北交所 | `bj430...` 或 `8...` / `4...` | `430xxx.BJ` |
| 港股 | `hk00700` 或 `hk09988` | `00700.HK` |
