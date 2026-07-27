# Tushare Pro 接口能力实测（2026-06-23 ~ 2026-07-23 区间）

> **结论先行：本次实测 Tushare 数据源整体不可用。** 项目 `pyserver/.env` 配置的 Tushare 代理（`TUSHARE_HTTP_URL`，tu.brze.top）上游 tenant key 已过期，全部数据接口返回 `tenant key expired`；同一 token 在 Tushare 官方端点（api.tushare.pro、api.waditu.com/dataapi）校验失败（40101「您的token不对」）。因此本次没有产生任何成功的数据拉取，股票池分析改用降级数据源（见 pool-performance-1m.md 的数据源声明）。

## 测试方法

- 直接调用 `.env` 中配置的 Tushare HTTP 端点（tushare 客户端实际请求路径 `{base}/{api_name}`），token 仅从 `pyserver/.env` 读取用于请求，未打印、未写入任何文件。
- 未启动、未修改 pyserver 服务。
- 每个接口以代表性参数各调用 1 次（个股代表：688256.SH 寒武纪；指数代表：000300.SH），记录 HTTP 可达性、耗时、返回码与错误信息。

## 实测结果表

| 接口 | 用途 | 可达性 | 耗时(s) | 返回码 | 返回信息/字段 | 限制记录 |
|---|---|---|---|---|---|---|
| trade_cal | 交易日历（探活基准） | ✅ HTTP可达 | 0.13 | 50002 | tenant key expired (contact admin to renew) | 上游tenant key过期 |
| daily | 日线行情 OHLC/涨跌/成交额 | ✅ HTTP可达 | 0.19 | 50002 | tenant key expired (contact admin to renew) | 上游tenant key过期 |
| adj_factor | 复权因子 | ✅ HTTP可达 | 0.15 | 50002 | tenant key expired (contact admin to renew) | 上游tenant key过期 |
| daily_basic | 换手率/量比/市值 | ✅ HTTP可达 | 0.14 | 50002 | tenant key expired (contact admin to renew) | 上游tenant key过期 |
| moneyflow | 个股资金流向 | ✅ HTTP可达 | 0.13 | 50002 | tenant key expired (contact admin to renew) | 上游tenant key过期 |
| fina_indicator | 财务指标（财务类代表） | ✅ HTTP可达 | 0.13 | 50002 | tenant key expired (contact admin to renew) | 上游tenant key过期 |
| index_daily | 指数日线（沪深300/创业板指） | ✅ HTTP可达 | 0.11 | 50007 | server refused the connection due to multiple unauthorized access atte | 反滥用临时拒绝(约256s) |

## 交叉验证记录

- `https://api.tushare.pro`（官方经典端点）：40101「您的token不对，请确认。」——该 token 为代理服务签发，非官方原生 token，官方端点不可用。
- `https://api.waditu.com/dataapi/trade_cal`（官方新端点）：同上，40101。
- 代理根路径 `/` 与 `/docs`、`/openapi.json` 可达（FastAPI tushare-proxy v2.0），说明网络与代理服务本身在线，故障点为上游租户密钥过期。
- 积分/限流报错：本次未触发积分不足类报错（如「您的积分不够」）；触发了代理侧反滥用临时拒绝（code 50007，剩余约 256 秒），由连续错误路径探测引起，冷却后可恢复但数据接口仍因 tenant key 过期不可用。

## 字段完整度

所有接口均未返回数据行（rows=0），无法评估字段完整度。待数据源恢复后需重跑本实测。

## 建议

1. 联系代理服务管理员续期 tenant key，或更换为 Tushare 官方原生 token（需相应积分：daily_basic/moneyflow/fina_indicator 通常要求 2000+ 积分）。
2. 恢复后重跑本基准脚本，再校验 pyserver 侧车链路。
3. 在 pyserver 健康检查中增加对代理错误的显式上报，避免静默降级。