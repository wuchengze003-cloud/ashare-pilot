import Link from "next/link";
import { readRuntimeJson } from "@/lib/runtimeData";
import { readSignalHistorySnapshots } from "@/lib/signalHistory";
import { buildBuySignalHistoryRows } from "@/lib/buySignalHistory";
import { loadEntries } from "@/lib/universe";
import { STRATEGIES } from "@/lib/strategyRegistry";
import { BuySignalHistoryTable } from "./BuySignalHistoryTable";
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

function numberOrDash(v: number | null | undefined, digits = 2) {
  return v == null || Number.isNaN(v) ? "暂无" : v.toFixed(digits);
}

function pctOrDash(v: number | null | undefined, digits = 1) {
  return v == null || Number.isNaN(v) ? "暂无" : pct(v, digits);
}

function sideLabel(side: "buy" | "sell" | "reduce") {
  if (side === "buy") return "买入";
  if (side === "reduce") return "减仓";
  return "卖出";
}

function shadowActionLabel(action: "buy" | "hold" | "sell" | "cash") {
  if (action === "buy") return "候选买入";
  if (action === "sell") return "候选卖出";
  if (action === "cash") return "持币";
  return "候选持有";
}

export default function DashboardPage() {
  const data = loadDashboardData();
  const universe = loadEntries();
  const analyst = readRuntimeJson<{
    items?: Array<{ symbol: string; current_price?: number | null; current_price_as_of?: string | null }>;
  }>("analyst.json");
  const history = readSignalHistorySnapshots(Number.POSITIVE_INFINITY);
  const nameMap = new Map(universe.map((e) => [e.symbol, e.name]));
  const themeMap = new Map(universe.map((e) => [e.symbol, e.theme]));
  const currentPriceBySymbol = new Map((analyst?.items ?? []).map((item) => [item.symbol, item.current_price ?? null]));
  const currentAsOfBySymbol = new Map((analyst?.items ?? []).map((item) => [item.symbol, item.current_price_as_of ?? null]));

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
  const shadowModel = data?.latestPlan?.shadowModel;
  const championModel = data?.latestPlan?.championModel;
  const researchStatus = data?.researchStatus;
  const latestAssessment = researchStatus?.promotion_assessments?.at(-1);
  const fiveDayFeedback = researchStatus?.outcome_feedback?.summary?.groups
    ?.filter((item) => item.horizon_bars === 5)
    .at(-1);
  const shadowEquityByDate = new Map(
    (shadowModel?.shadow_account?.equity_curve ?? []).map((point) => [point.date, point.equity]),
  );
  const equityData = data
    ? data.equityCurve.map((point, index) => ({
        date: point.date,
        equity: point.equity,
        benchmark: data.benchmarkCurve[index]?.equity ?? null,
        shadow: shadowEquityByDate.get(point.date) ?? null,
      }))
    : [];
  const decisionBasisLabel = data?.snapshot_basis === "intraday-midday" ? "午盘快照决策" : "收盘决策";
  const targetBuys = data?.latestPlan?.signals.filter((s) => s.action === "buy" && s.size > 0) ?? [];
  const signalMap = new Map((data?.latestPlan?.signals ?? []).map((s) => [s.symbol, s]));
  const targetSymbols = new Set(targetBuys.map((s) => s.symbol));
  const plannedHoldings = holdings
    .map((h) => ({
      symbol: h.sym,
      signal: signalMap.get(h.sym),
      currentWeight: latestEquity > 0 ? h.value / latestEquity : 0,
    }))
    .map((h) => ({
      symbol: h.symbol,
      side: h.signal?.action === "sell" ? "sell" as const : h.signal?.action === "buy" ? "buy" as const : "hold" as const,
      label: h.signal?.action === "sell" ? "卖出" : "持有",
      targetWeight: h.signal?.action === "sell"
        ? 0
        : h.signal?.action === "buy"
          ? h.signal.size
          : h.currentWeight,
      reason: h.signal?.rationale ?? "未生成信号",
    }));
  const heldSymbols = new Set(holdings.map((h) => h.sym));
  const plannedBuys = targetBuys
    .filter((s) => !heldSymbols.has(s.symbol))
    .map((s) => ({
      symbol: s.symbol,
      side: "buy" as const,
      label: "买入",
      targetWeight: s.size,
      reason: cashWeight < s.size ? `现金不足，需先卖出释放仓位；${s.rationale}` : s.rationale,
    }));
  const plannedOrders = [...plannedHoldings, ...plannedBuys];
  const buySignalHistory = buildBuySignalHistoryRows(
    history,
    currentPriceBySymbol,
    currentAsOfBySymbol,
  ).map((signal) => ({
    ...signal,
    name: signal.name ?? nameMap.get(signal.symbol) ?? null,
    theme: signal.theme ?? themeMap.get(signal.symbol) ?? null,
  }));

  return (
    <div className="container">
      <Link href="/" className="back-link" data-umami-event="nav-home">返回股票池</Link>
      <header className="page-header compact">
        <div>
          <div className="eyebrow">Dashboard</div>
          <h1>策略 Dashboard</h1>
          <p>
            {data?.strategy
              ? `当前策略：${data.strategy.name} (${data.strategy.codename}) — ${data.strategy.description}`
              : "基于项目股票池的可复现规则回测。数据来自本地行情侧车缓存，信号按当前运行快照生成。"}
          </p>
          <div className="strategy-tabs" style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
            {STRATEGIES.map((s) => (
              <span
                key={s.id}
                className={`strategy-tab${data?.strategy?.id === s.id ? " active" : ""}`}
                style={{
                  padding: "4px 12px",
                  borderRadius: 6,
                  fontSize: 13,
                  border: "1px solid var(--border, #333)",
                  background: data?.strategy?.id === s.id ? "var(--accent, #4f8cff)" : "transparent",
                  color: data?.strategy?.id === s.id ? "#fff" : "var(--muted, #999)",
                  cursor: "default",
                }}
                title={s.description}
              >
                {s.name} <small style={{ opacity: 0.7 }}>{s.codename}</small>
              </span>
            ))}
          </div>
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
            {data.stats.roundTrips != null && (
              <Kpi label="完整交易" value={`${data.stats.roundTrips} 笔`} />
            )}
            {data.stats.roundTripWinRatePct != null && (
              <Kpi label="交易胜率" value={pct(data.stats.roundTripWinRatePct)} pos={data.stats.roundTripWinRatePct >= 50} />
            )}
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

          <h2 className="subheading">模型状态</h2>
          <div className="theme-panel">
            <div className="theme-title">
              <strong>{championModel ? "ML 正式策略" : "V1 正式策略"} / ML 影子策略</strong>
              <span>
                {shadowModel
                  ? `${shadowModel.model_version} · 影子，不参与交易`
                  : championModel
                    ? `${championModel.model_version} · 已通过晋级`
                  : "尚无通过数据校验的 ML 预测"}
              </span>
            </div>
            <div className="model-status-grid">
              <span>
                生产策略：{researchStatus?.production_strategy === "ml-champion" ? "ML 正式模型" : "V1 规则"}
              </span>
              <span>
                候选模型：{researchStatus?.challenger_models?.length ?? 0}
              </span>
              <span>
                公开基准：{researchStatus?.qlib_benchmark?.passed
                  ? `${researchStatus.qlib_benchmark.data_cutoff} 可用，不可晋级`
                  : "未就绪"}
              </span>
              {researchStatus?.qlib_benchmark?.results?.linear?.median_rank_ic != null ? (
                <span>
                  Alpha158 线性 6 折中位 RankIC：
                  {researchStatus.qlib_benchmark.results.linear.median_rank_ic.toFixed(4)}
                </span>
              ) : null}
              <span className={researchStatus?.tushare_production?.passed ? "pos" : "neg"}>
                生产研究数据：{researchStatus?.tushare_production?.passed
                  ? `通过 · ${researchStatus.tushare_production.data_cutoff ?? "截止日未知"} · ${researchStatus.tushare_production.trading_days ?? 0} 日`
                  : "未通过"}
              </span>
              <span className={latestAssessment?.passed ? "pos" : "muted"}>
                晋级状态：{researchStatus?.activation_pending
                  ? `${researchStatus.activation_pending} 下一决策日生效`
                  : latestAssessment?.status === "promoted"
                    ? "已晋级"
                    : latestAssessment?.status === "eligible"
                      ? "符合门槛，待激活"
                      : latestAssessment
                        ? "影子验证中"
                        : "暂无候选"}
              </span>
            </div>
            {latestAssessment ? (
              <div className="promotion-summary">
                <span>
                  影子样本 <strong>{latestAssessment.metrics?.shadow_trading_days ?? 0}</strong>/60 日
                </span>
                <span>
                  完成交易 <strong>{latestAssessment.metrics?.closed_trades ?? 0}</strong>/20 笔
                </span>
                <span>
                  验收 Sharpe <strong>{numberOrDash(latestAssessment.metrics?.primary_sharpe)}</strong>/3.00
                </span>
                <span>
                  OOS 折数 <strong>{latestAssessment.metrics?.oos_folds ?? 0}</strong>/6
                </span>
                <span className={latestAssessment.failures?.length ? "neg" : "pos"}>
                  {latestAssessment.failures?.length
                    ? `未通过：${latestAssessment.failures.slice(0, 3).map((failure) => failure.code).join("、")}`
                    : "全部晋级门槛通过"}
                </span>
                {researchStatus?.model_health ? (
                  <span>
                    冠军健康：连续跑输 {researchStatus.model_health.consecutive_underperform_days ?? 0}/10 日
                    · 当前回撤 {pctOrDash(researchStatus.model_health.current_drawdown_pct)}
                  </span>
                ) : null}
                {fiveDayFeedback ? (
                  <span>
                    5日奖惩：{fiveDayFeedback.observations ?? 0} 个样本
                    · 超额命中 {pctOrDash((fiveDayFeedback.hit_rate ?? 0) * 100)}
                    · 校准误差 {pctOrDash((fiveDayFeedback.mean_absolute_calibration_error ?? 0) * 100)}
                  </span>
                ) : null}
              </div>
            ) : null}
            {!shadowModel ? (
              <p className="muted" style={{ padding: "12px 16px", margin: 0 }}>
                {championModel
                  ? `当前交易计划由 ${championModel.model_version} 生成，暂无新的影子挑战模型。`
                  : "当前买卖、持仓和回测仍全部由 V1 规则生成。研究模型完成训练和影子验证后才会在此显示。"}
              </p>
            ) : (
              <div className="table-wrap compact-table">
                <table>
                  <thead>
                    <tr>
                      <th>排名</th>
                      <th>代码</th>
                      <th>名称</th>
                      <th>影子动作</th>
                      <th className="num">3日超额</th>
                      <th className="num">下行风险</th>
                      <th>主要驱动</th>
                      <th className="num">目标仓位</th>
                    </tr>
                  </thead>
                  <tbody>
                    {shadowModel.predictions.slice(0, 8).map((prediction) => (
                      <tr key={`shadow-${prediction.symbol}`}>
                        <td className="mono">{prediction.rank}</td>
                        <td className="mono">{prediction.symbol}</td>
                        <td>{nameMap.get(prediction.symbol) ?? "名称未收录"}</td>
                        <td>{shadowActionLabel(prediction.action)}</td>
                        <td className={`num ${(prediction.expectedReturns.d3 ?? 0) >= 0 ? "pos" : "neg"}`}>
                          {prediction.expectedReturns.d3 == null ? "暂无" : pct(prediction.expectedReturns.d3 * 100)}
                        </td>
                        <td className="num neg">{pct(prediction.downsideRisk * 100)}</td>
                        <td className="mono">
                          {Object.entries(prediction.featureContributions ?? {})
                            .slice(0, 2)
                            .map(([name, value]) => `${name} ${pct(value * 100, 1)}`)
                            .join(" · ") || "暂无"}
                        </td>
                        <td className="num">{pct(prediction.targetWeight * 100, 1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="muted" style={{ padding: "10px 16px" }}>
                  数据截止 {shadowModel.data_cutoff} · 特征 {shadowModel.feature_version}
                  {shadowModel.quality?.warnings.length
                    ? ` · 警告：${shadowModel.quality.warnings.join("；")}`
                    : " · 数据质量与漂移检查通过"}
                </div>
              </div>
            )}
          </div>

          <h2 className="subheading">权益曲线对比</h2>
          <div className="card chart-card">
            <EquityChart data={equityData} />
          </div>

          <h2 className="subheading">主题配置与收益贡献</h2>
          <div className="card chart-card">
            <ThemeChart data={themeData} />
          </div>

          <div className="theme-grid dashboard-grid" style={{ marginTop: 16 }}>
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
                <table className="plan-table">
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

            <div className="theme-panel recent-trades-panel">
              <div className="theme-title"><strong>最近交易</strong><span>近 10 / 共 {data.trades.length} 笔</span></div>
              <div className="table-wrap compact-table">
                <table className="recent-table">
                  <thead>
                    <tr><th>决策日</th><th>成交日</th><th>代码</th><th>方向</th><th className="num">数量</th><th className="num">价格</th></tr>
                  </thead>
                  <tbody>
                    {data.trades.slice(-10).reverse().map((t, i) => (
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

            <div className="theme-panel signal-summary-panel">
              <div className="theme-title">
                <strong>近期信号表现</strong>
                <span>{history.length} 个交易日 · 最新现价</span>
              </div>
              <div className="table-wrap compact-table">
                <table className="signal-summary-table">
                  <thead>
                    <tr>
                      <th>信号日</th>
                      <th>名称</th>
                      <th className="num">信号价</th>
                      <th className="num">现价</th>
                      <th className="num">涨跌</th>
                    </tr>
                  </thead>
                  <tbody>
                    {buySignalHistory.length === 0 && (
                      <tr><td colSpan={5} className="muted">暂无历史信号归档</td></tr>
                    )}
                    {buySignalHistory.slice(0, 10).map((signal) => (
                      <tr key={`summary-${signal.signalDate}-${signal.symbol}`}>
                        <td className="mono">{signal.signalDate}</td>
                        <td>{signal.name ?? nameMap.get(signal.symbol) ?? signal.symbol}</td>
                        <td className="num">{numberOrDash(signal.signalPrice)}</td>
                        <td className="num">{numberOrDash(signal.currentPrice)}</td>
                        <td className={`num ${signal.changePct == null ? "muted" : signal.changePct >= 0 ? "pos" : "neg"}`}>
                          {pctOrDash(signal.changePct)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>

          <h2 className="subheading">完整历史买入信号表现</h2>
          <BuySignalHistoryTable
            rows={buySignalHistory}
            archiveDates={history.map((snapshot) => snapshot.signal_date)}
          />
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
