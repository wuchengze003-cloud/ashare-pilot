import Link from "next/link";
import { readRuntimeJson } from "@/lib/runtimeData";
import { loadEntries } from "@/lib/universe";
import { EquityChart, ThemeChart } from "./Charts";
import type { DashboardData } from "./types";

export const dynamic = "force-dynamic";

function loadDashboardData(): DashboardData | null {
  return readRuntimeJson<DashboardData>("backtest.json");
}

function pct(v: number, digits = 2) {
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

function money(v: number) {
  return `¥${Math.round(v).toLocaleString("en-US")}`;
}

function sideLabel(side: "buy" | "sell" | "reduce") {
  if (side === "buy") return "买入";
  if (side === "reduce") return "减仓";
  return "卖出";
}

function plannedDirectionLabel(side: "buy" | "sell" | "hold", isHeld: boolean) {
  if (side === "sell") return "卖出";
  if (side === "hold") return "持有";
  return isHeld ? "调仓" : "买入";
}

export default function DashboardPage() {
  const data = loadDashboardData();
  const universe = loadEntries();
  const nameMap = new Map(universe.map((e) => [e.symbol, e.name]));
  const themeMap = new Map(universe.map((e) => [e.symbol, e.theme]));

  const equityData = data
    ? data.equityCurve.map((b, i) => ({
        date: b.date,
        equity: b.equity,
        benchmark: data.benchmarkCurve[i]?.equity ?? null,
      }))
    : [];

  const themeData = data
    ? data.themePerformance
        .map((t) => ({
          theme: t.theme,
          returnPct: Number(t.returnPct.toFixed(2)),
          realizedPct: Number(t.realizedPct.toFixed(2)),
          unrealizedPct: Number(t.unrealizedPct.toFixed(2)),
          avgWeightPct: Number(t.avgWeightPct.toFixed(2)),
        }))
        .sort((a, b) => b.returnPct - a.returnPct)
    : [];

  const hasBenchmark = Boolean(data?.benchmarkCurve.length);
  const benchmarkFinalEquity = data?.benchmarkCurve.at(-1)?.equity ?? null;
  const benchmarkReturnPct =
    data && hasBenchmark && benchmarkFinalEquity != null
      ? ((benchmarkFinalEquity / data.config.startCash) - 1) * 100
      : null;
  const sharpeTarget = data?.config.sharpeTarget ?? 3;
  const meetsSharpeTarget = data ? data.meetsSharpeTarget ?? data.stats.sharpe >= sharpeTarget : false;
  const decisionEveryNDays = data ? data.config.decisionEveryNDays ?? data.config.rebalanceEveryNDays : 1;
  const decisionCadenceLabel = decisionEveryNDays <= 1 ? "每日" : `每 ${decisionEveryNDays} 日`;
  const latestBar = data?.equityCurve.at(-1) ?? null;
  const holdings = data
    ? Object.entries(data.latestHoldings)
        .map(([sym, pos]) => ({
          sym,
          pos,
          value: pos.shares * pos.price,
        }))
        .sort((a, b) => b.value - a.value)
    : [];
  const holdingsValue = holdings.reduce((sum, item) => sum + item.value, 0);
  const latestCash = latestBar?.cash ?? 0;
  const latestEquity = latestBar?.equity ?? holdingsValue + latestCash;
  const cashWeight = latestEquity > 0 ? latestCash / latestEquity : 0;
  const snapshotLabel = data?.snapshot_label ?? "完整收盘";
  const decisionBasisLabel = data?.snapshot_basis === "intraday-midday" ? "午盘快照决策" : "收盘决策";
  const targetBuys = data?.latestPlan?.signals.filter((s) => s.action === "buy" && s.size > 0) ?? [];
  const targetSymbols = new Set(targetBuys.map((s) => s.symbol));
  const hardSellSignals = new Map(
    (data?.latestPlan?.signals ?? [])
      .filter((s) => s.action === "sell")
      .map((s) => [s.symbol, s]),
  );
  const barIndex = new Map((data?.equityCurve ?? []).map((bar, index) => [bar.date, index] as const));
  const latestBarIndex = data ? barIndex.get(data.latestDate) ?? data.equityCurve.length - 1 : 0;
  const nextTradeBarIndex = latestBarIndex + 1;
  const minHoldBars = data?.config.minHoldBars ?? 0;
  const heldBarsAtNextOpen = (symbol: string) => {
    if (!data) return Number.POSITIVE_INFINITY;
    const lastBuy = [...data.trades].reverse().find((t) => t.symbol === symbol && t.side === "buy");
    const lastBuyIndex = lastBuy ? barIndex.get(lastBuy.tradeDate ?? lastBuy.date) : undefined;
    return lastBuyIndex == null ? Number.POSITIVE_INFINITY : nextTradeBarIndex - lastBuyIndex;
  };
  const plannedSells = holdings
    .filter((h) => !targetSymbols.has(h.sym))
    .map((h) => ({
      symbol: h.sym,
      hardSell: hardSellSignals.get(h.sym),
      heldBars: heldBarsAtNextOpen(h.sym),
      currentWeight: latestEquity > 0 ? h.value / latestEquity : 0,
    }))
    .map((h) => {
      const canOrdinarySell = minHoldBars <= 0 || h.heldBars >= minHoldBars;
      if (!h.hardSell && !canOrdinarySell) {
        return {
          symbol: h.symbol,
          side: "hold" as const,
          label: plannedDirectionLabel("hold", true),
          targetWeight: h.currentWeight,
          reason: `跌出目标组合，但未满最短持仓 ${minHoldBars} 日，暂不轮动卖出`,
        };
      }
      return {
        symbol: h.symbol,
        side: "sell" as const,
        label: plannedDirectionLabel("sell", true),
        targetWeight: 0,
        reason: h.hardSell?.rationale ?? "跌出明日目标组合，次日开盘优先卖出",
      };
    });
  const plannedBuys = targetBuys.map((s) => ({
    symbol: s.symbol,
    side: "buy" as const,
    label: plannedDirectionLabel("buy", holdings.some((h) => h.sym === s.symbol)),
    targetWeight: s.size,
    reason: s.rationale,
  }));
  const plannedOrders = [...plannedSells, ...plannedBuys];

  return (
    <div className="container">
      <Link href="/" className="back-link">返回股票池</Link>
      <header className="page-header compact">
        <div>
          <div className="eyebrow">Dashboard</div>
          <h1>策略 Dashboard</h1>
          <p>
            基于项目股票池的可复现规则回测；当前生成脚本默认使用右侧价格-主题-量能规则。
            数据来自本地行情侧车缓存，信号按当前运行快照生成。
          </p>
        </div>
      </header>

      {!data && (
        <div className="card" style={{ borderColor: "var(--warn)" }}>
          <strong>尚未生成回测数据</strong>
          <p style={{ color: "var(--muted)" }}>
            收盘后运行 <code>cd web && npm run dashboard:update</code>。
            该命令会刷新行情缓存、重建量化模拟仓和明日计划，并写入 ignored 的{" "}
            <code>web/data/runtime</code>。
          </p>
        </div>
      )}

      {data && (
        <>
          <div className="row" style={{ marginTop: 16 }}>
            <Kpi label="总收益" value={pct(data.stats.totalReturnPct)} pos={data.stats.totalReturnPct >= 0} />
            <Kpi label="年化" value={pct(data.stats.cagrPct)} pos={data.stats.cagrPct >= 0} />
            <Kpi label="最大回撤" value={pct(data.stats.maxDrawdownPct)} pos={false} />
            <Kpi label="夏普" value={data.stats.sharpe.toFixed(2)} pos={meetsSharpeTarget} />
            <Kpi label="交易次数" value={data.stats.trades.toString()} />
            <Kpi label="胜率" value={data.stats.winRatePct == null ? "暂无" : pct(data.stats.winRatePct)} pos={(data.stats.winRatePct ?? 0) >= 50} />
            <Kpi label="换手" value={data.stats.turnoverPct == null ? "暂无" : pct(data.stats.turnoverPct, 0)} />
            <Kpi
              label="沪深300基准"
              value={benchmarkReturnPct == null ? "暂无数据" : pct(benchmarkReturnPct)}
              pos={benchmarkReturnPct == null ? undefined : benchmarkReturnPct >= 0}
            />
          </div>

          <div className="row" style={{ marginTop: 8, fontSize: 12, color: "var(--muted)" }}>
            <span>回测区间 {data.config.startDate} → {data.config.endDate}</span>
            <span>·</span>
            <span>{decisionCadenceLabel}{decisionBasisLabel}，次日开盘成交</span>
            <span>·</span>
            <span>{snapshotLabel}</span>
            <span>·</span>
            <span>最大持仓 {data.config.maxPositions} 只</span>
            <span>·</span>
            <span>手续费 {data.config.feeBps} bps</span>
            <span>·</span>
            <span>
              参数：最短持仓 {data.config.minHoldBars ?? "未设"} 日，换手阈值 {data.config.rebalanceThresholdPct ?? 0}%
              {data.optimizedParams ? `，买入分数 ${data.optimizedParams.minScoreToBuy.toFixed(2)}` : ""}
            </span>
            <span>·</span>
            <span>生成于 {new Date(data.generated_at).toLocaleString("zh-CN")}</span>
          </div>
          {meetsSharpeTarget ? (
            <div className="status-strip good">
              Sharpe 目标已达标：当前 {data.stats.sharpe.toFixed(2)} / 目标 {sharpeTarget.toFixed(1)}。
            </div>
          ) : (
            <div className="warning-strip">
              Sharpe 目标未达标：当前 {data.stats.sharpe.toFixed(2)} / 目标 {sharpeTarget.toFixed(1)}。
            </div>
          )}

          <h2 className="subheading">权益曲线 vs 沪深300</h2>
          <div className="card chart-card">
            <EquityChart data={equityData} />
          </div>

          <h2 className="subheading">主题配置与收益贡献</h2>
          <div className="card chart-card">
            <ThemeChart data={themeData} />
          </div>

          <div className="theme-grid" style={{ marginTop: 16 }}>
            <div className="theme-panel">
              <div className="theme-title">
                <strong>量化模拟仓</strong>
                <span>
                  {data.latestDate} · {holdings.length} 只 · 股票 {money(holdingsValue)} · 现金 {money(latestCash)}
                </span>
              </div>
              <div className="table-wrap compact-table">
                <table>
                  <thead>
                    <tr>
                      <th>代码</th>
                      <th>名称</th>
                      <th>主题</th>
                      <th className="num">数量</th>
                      <th className="num">价格</th>
                      <th className="num">市值</th>
                      <th className="num">权重</th>
                    </tr>
                  </thead>
                  <tbody>
                    {holdings.length === 0 && (
                      <tr><td colSpan={7} className="muted">空仓</td></tr>
                    )}
                    {holdings.map(({ sym, pos, value }) => (
                      <tr key={sym}>
                        <td className="mono">{sym}</td>
                        <td>{nameMap.get(sym) ?? "名称未收录"}</td>
                        <td>{themeMap.get(sym) ?? "主题未收录"}</td>
                        <td className="num">{Math.round(pos.shares).toLocaleString("en-US")}</td>
                        <td className="num">{pos.price.toFixed(2)}</td>
                        <td className="num">{money(value)}</td>
                        <td className="num">{((value / (latestEquity || 1)) * 100).toFixed(1)}%</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="theme-panel">
              <div className="theme-title">
                <strong>明日目标信号</strong>
                <span>{data.latestPlan?.decisionDate ?? data.latestDate} 收盘信号 · 现金 {((cashWeight || 0) * 100).toFixed(1)}%</span>
              </div>
              <div className="table-wrap compact-table">
                <table>
                  <thead>
                    <tr>
                      <th>代码</th>
                      <th>名称</th>
                      <th>方向</th>
                      <th className="num">仓位</th>
                      <th>原因</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plannedOrders.length === 0 && (
                      <tr><td colSpan={5} className="muted">暂无待执行换仓</td></tr>
                    )}
                    {plannedOrders.map((order) => (
                      <tr key={`${order.side}-${order.symbol}`}>
                        <td className="mono">{order.symbol}</td>
                        <td>{nameMap.get(order.symbol) ?? "名称未收录"}</td>
                        <td><span className={`badge ${order.side}`}>{order.label}</span></td>
                        <td className="num">{(order.targetWeight * 100).toFixed(1)}%</td>
                        <td className="muted signal-reason">{order.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            <div className="theme-panel">
              <div className="theme-title"><strong>最近交易</strong><span>共 {data.trades.length} 笔</span></div>
              <div className="table-wrap compact-table">
                <table>
                  <thead>
                    <tr><th>决策日</th><th>成交日</th><th>代码</th><th>方向</th><th className="num">数量</th><th className="num">价格</th></tr>
                  </thead>
                  <tbody>
                    {data.trades.slice(-20).reverse().map((t, i) => (
                      <tr key={i}>
                        <td>{t.decisionDate ?? t.date}</td>
                        <td>{t.tradeDate ?? t.date}</td>
                        <td className="mono">{t.symbol}</td>
                        <td><span className={`badge ${t.side}`}>{sideLabel(t.side)}</span></td>
                        <td className="num">{t.shares}</td>
                        <td className="num">{t.price.toFixed(2)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Kpi({ label, value, pos }: { label: string; value: string; pos?: boolean }) {
  return (
    <div className="kpi">
      <span className="label">{label}</span>
      <span className={`value ${pos === undefined ? "" : pos ? "pos" : "neg"}`}>{value}</span>
    </div>
  );
}
