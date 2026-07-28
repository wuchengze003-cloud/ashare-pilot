import Link from "next/link";
import { readRuntimeJson, readStrategyJson } from "@/lib/runtimeData";
import { loadEntries } from "@/lib/universe";
import { STRATEGIES } from "@/lib/strategyRegistry";
import { ComparisonEquityChart } from "./Charts";
import type { DashboardData } from "./types";

export const dynamic = "force-dynamic";

interface StrategyComparisonEntry {
  id: string;
  name: string;
  totalReturnPct: number;
  cagrPct: number;
  maxDrawdownPct: number;
  sharpe: number;
  trades: number;
  winRatePct?: number;
  turnoverPct?: number;
}

interface StrategyComparison {
  generated_at: string;
  data_date: string;
  strategies: StrategyComparisonEntry[];
}

function pct(v: number, digits = 2) {
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

function money(v: number) {
  return `¥${Math.round(v).toLocaleString("en-US")}`;
}

function numberOrDash(v: number | null | undefined, digits = 2) {
  return v == null || Number.isNaN(v) ? "暂无" : v.toFixed(digits);
}

/**
 * Compute pairwise return correlation between two equity curve series,
 * aligned by date. Returns null if insufficient overlap.
 */
function correlation(
  seriesA: Array<{ date: string; equity: number }>,
  seriesB: Array<{ date: string; equity: number }>,
): number | null {
  const mapA = new Map(seriesA.map((p) => [p.date, p.equity]));
  const mapB = new Map(seriesB.map((p) => [p.date, p.equity]));
  const dates = [...mapA.keys()].filter((d) => mapB.has(d)).sort();
  if (dates.length < 5) return null;

  // Convert to daily returns
  const retA: number[] = [];
  const retB: number[] = [];
  for (let i = 1; i < dates.length; i++) {
    const prevA = mapA.get(dates[i - 1])!;
    const curA = mapA.get(dates[i])!;
    const prevB = mapB.get(dates[i - 1])!;
    const curB = mapB.get(dates[i])!;
    if (prevA > 0 && prevB > 0) {
      retA.push(curA / prevA - 1);
      retB.push(curB / prevB - 1);
    }
  }
  if (retA.length < 3) return null;

  const n = retA.length;
  const meanA = retA.reduce((s, v) => s + v, 0) / n;
  const meanB = retB.reduce((s, v) => s + v, 0) / n;
  let cov = 0;
  let varA = 0;
  let varB = 0;
  for (let i = 0; i < n; i++) {
    cov += (retA[i] - meanA) * (retB[i] - meanB);
    varA += (retA[i] - meanA) ** 2;
    varB += (retB[i] - meanB) ** 2;
  }
  const denom = Math.sqrt(varA * varB);
  return denom > 0 ? cov / denom : null;
}

export default function DashboardPage() {
  const comparison = readRuntimeJson<StrategyComparison>("strategy-comparison.json");
  const universe = loadEntries();
  const nameMap = new Map(universe.map((e) => [e.symbol, e.name]));

  // Load per-strategy data
  const strategyData: Array<{ meta: (typeof STRATEGIES)[number]; data: DashboardData | null }> = STRATEGIES.map(
    (meta) => ({
      meta,
      data: readStrategyJson<DashboardData>(meta.id, "backtest.json"),
    }),
  );

  const hasAnyData = strategyData.some((s) => s.data != null);

  // Build comparison equity curve overlay
  const allDates = new Set<string>();
  for (const { data } of strategyData) {
    if (!data) continue;
    for (const point of data.equityCurve) allDates.add(point.date);
  }
  const sortedDates = [...allDates].sort();
  const equityOverlay: Array<Record<string, string | number | null>> = sortedDates.map((date) => {
    const point: Record<string, string | number | null> = { date };
    for (const { meta, data } of strategyData) {
      if (!data) continue;
      const bar = data.equityCurve.find((p) => p.date === date);
      point[meta.id] = bar ? bar.equity : null;
    }
    // Use first available strategy's benchmark
    const firstWithBenchmark = strategyData.find((s) => s.data?.benchmarkCurve.some((p) => p.date === date));
    point.benchmark = firstWithBenchmark?.data?.benchmarkCurve.find((p) => p.date === date)?.equity ?? null;
    return point;
  });

  // Compute pairwise correlations
  const correlations: Array<{ a: string; b: string; value: number | null }> = [];
  for (let i = 0; i < strategyData.length; i++) {
    for (let j = i + 1; j < strategyData.length; j++) {
      const a = strategyData[i];
      const b = strategyData[j];
      correlations.push({
        a: a.meta.name,
        b: b.meta.name,
        value: a.data && b.data
          ? correlation(a.data.equityCurve, b.data.equityCurve)
          : null,
      });
    }
  }

  // Find common holdings across strategies
  const holdingsByStrategy = strategyData.map(({ meta, data }) => {
    const held = data ? new Set(Object.keys(data.latestHoldings)) : new Set<string>();
    return { meta, held };
  });
  const allHeldSymbols = new Set<string>();
  for (const { held } of holdingsByStrategy) {
    for (const sym of held) allHeldSymbols.add(sym);
  }
  const commonHoldings = [...allHeldSymbols]
    .map((sym) => ({
      symbol: sym,
      name: nameMap.get(sym) ?? "名称未收录",
      strategies: holdingsByStrategy
        .filter((h) => h.held.has(sym))
        .map((h) => h.meta.name),
      count: holdingsByStrategy.filter((h) => h.held.has(sym)).length,
    }))
    .filter((item) => item.count >= 2)
    .sort((a, b) => b.count - a.count);

  // Find common drawdown periods (both strategies drawdown > 5% on same date)
  const drawdownByStrategy = strategyData.map(({ meta, data }) => {
    if (!data) return { meta, drawdowns: new Map<string, number>() };
    const map = new Map<string, number>();
    let peak = 0;
    for (const point of data.equityCurve) {
      if (point.equity > peak) peak = point.equity;
      const dd = peak > 0 ? ((point.equity - peak) / peak) * 100 : 0;
      map.set(point.date, dd);
    }
    return { meta, drawdowns: map };
  });
  const commonDrawdownDates = sortedDates.filter((date) => {
    const dds = drawdownByStrategy
      .map((d) => d.drawdowns.get(date))
      .filter((v): v is number => v != null);
    return dds.length >= 2 && dds.every((dd) => dd < -5);
  });

  return (
    <div className="container">
      <Link href="/" className="back-link" data-umami-event="nav-home">返回首页</Link>
      <header className="page-header compact">
        <div>
          <div className="eyebrow">Dashboard</div>
          <h1>策略对比总览</h1>
          <p>
            三条策略共享同一股票池，独立持仓、独立权益曲线、独立交易明细。
            点击策略名称进入独立详情页面。
          </p>
          <div className="strategy-tabs" style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
            {STRATEGIES.map((s) => (
              <Link
                key={s.id}
                href={`/dashboard/${s.id}`}
                className="strategy-tab"
                style={{
                  padding: "4px 12px",
                  borderRadius: 6,
                  fontSize: 13,
                  border: "1px solid var(--border, #333)",
                  background: "transparent",
                  color: "var(--muted, #999)",
                  cursor: "pointer",
                  textDecoration: "none",
                  display: "inline-block",
                }}
                title={s.description}
              >
                {s.name} <small style={{ opacity: 0.7 }}>{s.codename}</small>
              </Link>
            ))}
          </div>
        </div>
      </header>

      {!hasAnyData && (
        <div className="card" style={{ borderColor: "var(--warn)" }}>
          <strong>尚未生成回测数据</strong>
          <p style={{ color: "var(--muted)" }}>
            收盘后运行 <code>cd web && npm run dashboard:update</code>。
            该命令会刷新行情缓存、重建三策略模拟仓和明日计划，并写入 ignored 的{" "}
            <code>web/data/runtime</code>。
          </p>
        </div>
      )}

      {hasAnyData && (
        <>
          {/* Strategy comparison table */}
          <h2 className="subheading">策略核心指标对比</h2>
          <div className="card" style={{ marginTop: 8 }}>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>策略</th>
                    <th>代号</th>
                    <th className="num">总收益</th>
                    <th className="num">年化</th>
                    <th className="num">最大回撤</th>
                    <th className="num">夏普</th>
                    <th className="num">交易次数</th>
                    <th className="num">胜率</th>
                    <th className="num">换手</th>
                    <th>详情</th>
                  </tr>
                </thead>
                <tbody>
                  {strategyData.map(({ meta, data }) => {
                    const stats = data?.stats;
                    const comp = comparison?.strategies.find((s) => s.id === meta.id);
                    return (
                      <tr key={meta.id}>
                        <td><strong>{meta.name}</strong></td>
                        <td className="mono">{meta.codename}</td>
                        <td className={`num ${stats ? (stats.totalReturnPct >= 0 ? "pos" : "neg") : ""}`}>
                          {stats ? pct(stats.totalReturnPct) : comp ? pct(comp.totalReturnPct) : "暂无"}
                        </td>
                        <td className={`num ${stats ? (stats.cagrPct >= 0 ? "pos" : "neg") : ""}`}>
                          {stats ? pct(stats.cagrPct) : comp ? pct(comp.cagrPct) : "暂无"}
                        </td>
                        <td className="num neg">
                          {stats ? pct(stats.maxDrawdownPct) : comp ? pct(comp.maxDrawdownPct) : "暂无"}
                        </td>
                        <td className="num">
                          {stats ? stats.sharpe.toFixed(2) : comp ? comp.sharpe.toFixed(2) : "暂无"}
                        </td>
                        <td className="num">
                          {stats ? stats.trades.toString() : comp ? comp.trades.toString() : "暂无"}
                        </td>
                        <td className="num">
                          {stats?.winRatePct != null ? pct(stats.winRatePct) : comp?.winRatePct != null ? pct(comp.winRatePct) : "暂无"}
                        </td>
                        <td className="num">
                          {stats?.turnoverPct != null ? pct(stats.turnoverPct, 0) : comp?.turnoverPct != null ? pct(comp.turnoverPct, 0) : "暂无"}
                        </td>
                        <td>
                          <Link href={`/dashboard/${meta.id}`} style={{ fontSize: 13 }}>
                            查看 →
                          </Link>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {comparison && (
              <div className="muted" style={{ padding: "8px 16px", fontSize: 12 }}>
                对比数据生成于 {new Date(comparison.generated_at).toLocaleString("zh-CN")}
                · 数据截止 {comparison.data_date}
              </div>
            )}
          </div>

          {/* Equity curve overlay */}
          <h2 className="subheading">权益曲线对比</h2>
          <div className="card chart-card">
            <ComparisonEquityChart
              data={equityOverlay}
              strategyIds={STRATEGIES.map((s) => s.id)}
            />
          </div>

          {/* Correlation matrix */}
          <h2 className="subheading">策略相关性</h2>
          <div className="card" style={{ marginTop: 8 }}>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>策略 A</th>
                    <th>策略 B</th>
                    <th className="num">日收益相关系数</th>
                    <th>解读</th>
                  </tr>
                </thead>
                <tbody>
                  {correlations.map((c, i) => (
                    <tr key={i}>
                      <td>{c.a}</td>
                      <td>{c.b}</td>
                      <td className={`num ${c.value == null ? "muted" : c.value > 0.5 ? "neg" : c.value < 0.2 ? "pos" : ""}`}>
                        {c.value == null ? "数据不足" : c.value.toFixed(3)}
                      </td>
                      <td className="muted" style={{ fontSize: 13 }}>
                        {c.value == null
                          ? "回测数据重叠不足"
                          : c.value > 0.7
                            ? "高度相关，分散收益有限"
                            : c.value > 0.4
                              ? "中等相关，有一定分散效果"
                              : c.value > 0.1
                                ? "低相关，分散效果较好"
                                : "接近独立，分散效果最佳"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Common holdings */}
          <h2 className="subheading">共同持仓</h2>
          <div className="card" style={{ marginTop: 8 }}>
            {commonHoldings.length === 0 ? (
              <p className="muted" style={{ padding: "16px" }}>当前无两策略以上共同持仓</p>
            ) : (
              <div className="table-wrap compact-table">
                <table>
                  <thead>
                    <tr>
                      <th>代码</th>
                      <th>名称</th>
                      <th>持有策略</th>
                      <th className="num">共持数</th>
                    </tr>
                  </thead>
                  <tbody>
                    {commonHoldings.map((h) => (
                      <tr key={h.symbol}>
                        <td className="mono">{h.symbol}</td>
                        <td>{h.name}</td>
                        <td>{h.strategies.join(" · ")}</td>
                        <td className="num">{h.count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {/* Common drawdown periods */}
          <h2 className="subheading">共同回撤区间</h2>
          <div className="card" style={{ marginTop: 8 }}>
            {commonDrawdownDates.length === 0 ? (
              <p className="muted" style={{ padding: "16px" }}>回测期间无两策略同时回撤超过 5% 的交易日</p>
            ) : (
              <>
                <p className="muted" style={{ padding: "8px 16px", fontSize: 13 }}>
                  以下交易日有 2 条以上策略同时处于 5% 以上回撤，共 {commonDrawdownDates.length} 日。
                  最近 20 日：
                </p>
                <div className="table-wrap compact-table">
                  <table>
                    <thead>
                      <tr>
                        <th>日期</th>
                        {strategyData.map(({ meta }) => (
                          <th key={meta.id} className="num">{meta.name} 回撤</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {commonDrawdownDates.slice(-20).reverse().map((date) => (
                        <tr key={date}>
                          <td className="mono">{date}</td>
                          {drawdownByStrategy.map(({ meta, drawdowns }) => (
                            <td key={meta.id} className="num neg">
                              {numberOrDash(drawdowns.get(date), 1)}%
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </div>

          {/* Strategy summary cards */}
          <h2 className="subheading">策略概要</h2>
          <div className="theme-grid" style={{ marginTop: 8 }}>
            {strategyData.map(({ meta, data }) => {
              const stats = data?.stats;
              const holdingsCount = data ? Object.keys(data.latestHoldings).length : 0;
              const latestEquity = data?.equityCurve.at(-1)?.equity ?? 0;
              const latestCash = data?.equityCurve.at(-1)?.cash ?? 0;
              return (
                <div key={meta.id} className="theme-panel">
                  <div className="theme-title">
                    <strong>{meta.name}</strong>
                    <span className="mono">{meta.codename}</span>
                  </div>
                  <p className="muted" style={{ fontSize: 13, margin: "8px 0" }}>{meta.description}</p>
                  <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13 }}>
                    <span>总收益 <strong className={stats ? (stats.totalReturnPct >= 0 ? "pos" : "neg") : ""}>{stats ? pct(stats.totalReturnPct) : "暂无"}</strong></span>
                    <span>夏普 <strong>{stats ? stats.sharpe.toFixed(2) : "暂无"}</strong></span>
                    <span>持仓 <strong>{holdingsCount} 只</strong></span>
                    <span>总权益 <strong>{data ? money(latestEquity) : "暂无"}</strong></span>
                    <span>现金 <strong>{data ? money(latestCash) : "暂无"}</strong></span>
                  </div>
                  <div style={{ marginTop: 12, display: "flex", gap: 8, flexWrap: "wrap" }}>
                    {meta.factors.map((f) => (
                      <span key={f} className="badge" style={{ fontSize: 11, padding: "2px 8px" }}>{f}</span>
                    ))}
                  </div>
                  <div style={{ marginTop: 12 }}>
                    <Link href={`/dashboard/${meta.id}`} style={{ fontSize: 14 }}>
                      进入 {meta.name} 详情 →
                    </Link>
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
