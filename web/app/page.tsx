import Link from "next/link";
import { readRuntimeJson } from "@/lib/runtimeData";
import { latestSignalDate, toExecutableSignals } from "@/lib/signalPolicy";
import { readUniverse } from "@/lib/universe";

export const dynamic = "force-dynamic";

interface UniverseEntry {
  symbol: string;
  name: string;
  theme: string;
  note?: string;
  global_supply?: boolean;
}

interface UniverseSnapshot {
  updated_at: string;
  updated_by: string;
  entries: UniverseEntry[];
}

interface AnalystItem {
  symbol: string;
  current_price?: number | null;
  current_price_source?: string | null;
  current_price_as_of?: string | null;
  implied_target?: number | null;
  target_price_source?: string | null;
  target_price_method?: string | null;
  target_price_confidence?: number | null;
  target_horizon_days?: number | null;
  upside_pct?: number | null;
  buy_count?: number | null;
  total_count?: number | null;
}

interface AnalystSnapshot {
  generated_at: string;
  items: AnalystItem[];
}

interface SignalItem {
  symbol: string;
  action: "buy" | "hold" | "sell";
  confidence: number;
  size: number;
  rationale: string;
}

interface BacktestSnapshot {
  generated_at: string;
  config: {
    startCash: number;
    rebalanceEveryNDays: number;
    decisionEveryNDays?: number;
    executionPrice?: "next_open";
    startDate: string;
    endDate: string;
    feeBps: number;
    maxPositions: number;
    minHoldBars?: number;
    rebalanceThresholdPct?: number;
    sharpeTarget?: number;
    optimizationWindow?: string;
  };
  stats: {
    totalReturnPct: number;
    cagrPct: number;
    maxDrawdownPct: number;
    sharpe: number;
    trades: number;
    winRatePct?: number;
    turnoverPct?: number;
  };
  equityCurve: Array<{ date: string; equity: number; cash?: number }>;
  trades: Array<{
    date: string;
    decisionDate?: string;
    tradeDate?: string;
    priceField?: "open";
    symbol: string;
    side: "buy" | "sell" | "reduce";
    shares: number;
    price: number;
    reason?: string;
    targetWeightBefore?: number;
    targetWeightAfter?: number;
    pnlPct?: number | null;
  }>;
  signalsByDate?: Record<string, SignalItem[]>;
  meetsSharpeTarget?: boolean;
  primaryWindow?: string;
  validationStats?: {
    jan_2026?: BacktestSnapshot["stats"];
  };
  optimizedParams?: {
    maxPositions: number;
    minHoldBars: number;
    rebalanceThresholdPct: number;
    minScoreToBuy: number;
  };
  optimizationWarnings?: string[];
}

interface SignalsSnapshot {
  generated_at: string;
  source: string;
  score_model: string;
  signal_date: string;
  signal_basis?: string;
  snapshot_label?: string;
  max_positions: number;
  spot_sources?: Record<string, number>;
  spot_as_of_min?: string | null;
  spot_as_of_max?: string | null;
  signals: SignalItem[];
}

interface MetaSnapshot {
  generated_at: string;
  universe_count: number;
}

function num(v: number | null | undefined, digits = 2, fallback = "未覆盖") {
  return v == null || Number.isNaN(v) ? fallback : v.toFixed(digits);
}

function pct(v: number | null | undefined, digits = 1, fallback = "未覆盖") {
  return v == null || Number.isNaN(v)
    ? fallback
    : `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

function money(v: number | null | undefined, fallback = "未覆盖") {
  return v == null || Number.isNaN(v)
    ? fallback
    : `¥${Math.round(v).toLocaleString("en-US")}`;
}

function actionLabel(action: SignalItem["action"] | undefined) {
  if (action === "buy") return "买入";
  if (action === "sell") return "卖出";
  if (action === "hold") return "不交易";
  return "未评分";
}

function sideLabel(side: "buy" | "sell" | "reduce") {
  if (side === "buy") return "买入";
  if (side === "reduce") return "减仓";
  return "卖出";
}

function priceFieldLabel(field: "open" | undefined) {
  return field === "open" ? "开盘" : "收盘";
}

function shortAsOf(value: string | null | undefined) {
  if (!value) return "未覆盖";
  return value.replace("T", " ").slice(0, 19);
}

function generatedLine(
  meta: MetaSnapshot,
  universe: UniverseSnapshot,
  marketDate: string,
  signalSnapshot: SignalsSnapshot,
) {
  const generated = new Date(meta.generated_at).toISOString().slice(0, 16).replace("T", " ");
  const signalBasis = signalSnapshot.signal_basis === "realtime-spot-merged"
    ? `实时信号：${signalSnapshot.signal_date} · 行情截至：${shortAsOf(signalSnapshot.spot_as_of_max)}`
    : signalSnapshot.signal_basis === "intraday-midday"
      ? `午盘快照：${signalSnapshot.signal_date}`
    : signalSnapshot.signal_basis === "latest-complete-close"
      ? `完整收盘：${signalSnapshot.signal_date}`
    : `信号日期：${signalSnapshot.signal_date}`;
  return `${signalBasis} · 回测截至：${marketDate} · 快照生成：${generated} UTC · 股票池更新：${universe.updated_at}`;
}

export default function Home() {
  const universe = readUniverse() as UniverseSnapshot;
  const analyst = readRuntimeJson<AnalystSnapshot>("analyst.json") ?? { generated_at: "", items: [] };
  const backtest = readRuntimeJson<BacktestSnapshot>("backtest.json");
  const signalSnapshot = readRuntimeJson<SignalsSnapshot>("signals.json");
  const meta = readRuntimeJson<MetaSnapshot>("meta.json") ?? {
    generated_at: new Date(0).toISOString(),
    universe_count: universe.entries.length,
  };

  if (!backtest || !signalSnapshot) {
    return (
      <div className="container">
        <header className="page-header">
          <div>
            <div className="eyebrow">Dashboard 规则评分 · 本地运行数据</div>
            <h1>硅基文明消费股交易系统</h1>
            <p>
              股票池已加载 {universe.entries.length} 只；当前尚未生成量化模拟仓和明日交易计划。
            </p>
            <p className="muted">股票池更新：{universe.updated_at}</p>
          </div>
        </header>
        <div className="warning-strip">
          尚未生成运行数据。收盘后运行 <code>cd web && npm run dashboard:update</code>，
          系统会写入 <code>web/data/runtime</code>，这些运行快照不会提交进 Git。
        </div>
      </div>
    );
  }

  const signalDate = signalSnapshot.signal_date ?? latestSignalDate(backtest.signalsByDate);
  const signals = toExecutableSignals(signalSnapshot.signals ?? [], {
    maxPositions: signalSnapshot.max_positions ?? backtest.config.maxPositions,
  });

  const analystBySymbol = new Map(analyst.items.map((a) => [a.symbol, a]));
  const signalBySymbol = new Map(signals.map((s) => [s.symbol, s]));
  const themes = [...new Set(universe.entries.map((e) => e.theme))].sort();
  const grouped = themes.map((theme) => ({
    theme,
    entries: universe.entries.filter((e) => e.theme === theme),
  }));
  const globalCount = universe.entries.filter((e) => e.global_supply).length;
  const targetItems = analyst.items.filter((a) => a.implied_target != null && a.target_price_source);
  const targetCount = targetItems.length;
  const upsideCount = targetItems.filter((a) => (a.upside_pct ?? 0) > 0).length;
  const buys = signals.filter((s) => s.action === "buy").length;
  const sells = signals.filter((s) => s.action === "sell").length;
  const marketDate = backtest.equityCurve.at(-1)?.date ?? backtest.config.endDate;
  const sharpeTarget = backtest.config.sharpeTarget ?? 3;
  const meetsSharpeTarget = backtest.meetsSharpeTarget ?? backtest.stats.sharpe >= sharpeTarget;
  const decisionEveryNDays = backtest.config.decisionEveryNDays ?? backtest.config.rebalanceEveryNDays;
  const decisionCadenceLabel = decisionEveryNDays <= 1 ? "每日" : `每 ${decisionEveryNDays} 日`;
  const orderedSignals = universe.entries
    .map((entry) => ({
      entry,
      signal: signalBySymbol.get(entry.symbol),
    }))
    .sort((a, b) => {
      const order = { buy: 0, sell: 1, hold: 2 };
      const ao = order[a.signal?.action ?? "hold"];
      const bo = order[b.signal?.action ?? "hold"];
      if (ao !== bo) return ao - bo;
      return (b.signal?.confidence ?? 0) - (a.signal?.confidence ?? 0);
    });
  const recentTrades = backtest.trades.slice().reverse().slice(0, 40);

  return (
    <div className="container">
      <header className="page-header">
        <div>
          <div className="eyebrow">Dashboard 规则评分 · easy-tdx · Tushare/AkShare · 本地运行快照</div>
          <h1>硅基文明消费股交易系统</h1>
          <p>
            算力芯片、光模块、AI 服务器、液冷、电力、电力设备、功率器件、IDC、存储/HBM、
            半导体设备与材料、AI-PCB、晶圆代工、云/AI基建等供给侧标的。
          </p>
          <p className="muted">{generatedLine(meta, universe, marketDate, signalSnapshot)}</p>
        </div>
        <nav className="header-actions" aria-label="页面导航">
          <a href="#universe" className="button secondary">股票池</a>
          <a href="#signals" className="button secondary">策略信号</a>
          <a href="#backtest" className="button secondary">回测</a>
          <Link href="/dashboard" className="button secondary">Dashboard</Link>
        </nav>
      </header>

      <div className="summary-grid">
        <Metric label="股票池" value={`${universe.entries.length}`} sub={`${themes.length} 个子主题`} />
        <Metric label="全球供应链" value={`${globalCount}`} sub={`${Math.round((globalCount / universe.entries.length) * 100)}% 覆盖`} />
        <Metric label="目标价覆盖" value={`${targetCount}`} sub={`${upsideCount} 只高于现价`} />
        <Metric label="明日计划" value={`${buys} 买 / ${sells} 卖`} sub={`信号日 ${signalDate ?? "未生成"}`} />
      </div>

      <section className="strategy-panel" aria-labelledby="strategy-title">
        <div className="strategy-panel-head">
          <div>
            <div className="eyebrow">Strategy policy</div>
            <h2 id="strategy-title">当前策略口径</h2>
          </div>
          <span className="pill good">每日收盘决策 · 次日开盘成交</span>
        </div>
        <div className="strategy-grid">
          <div>
            <span className="strategy-label">评分权重</span>
            <strong>价格 35% · 主题 30% · 成交量 20% · 趋势 15%</strong>
            <p>基本面只做风险过滤；明日买入只保留组合前 {signalSnapshot.max_positions ?? backtest.config.maxPositions} 只。</p>
          </div>
          <div>
            <span className="strategy-label">买入形态</span>
            <strong>突破确认 · 回踩转强 · 强主题内相对强势</strong>
            <p>要求可由收盘价、均线、量能和主题标签验证。</p>
          </div>
          <div>
            <span className="strategy-label">风控约束</span>
            <strong>不追单日 &gt;5% · 不追 5 日 &gt;18%</strong>
            <p>硬退出不受最短持仓限制；普通轮动用最短持仓抑制噪声。</p>
          </div>
        </div>
      </section>

      <section id="universe">
        <h2 className="subheading">股票池</h2>
        <p className="muted">分主题排列，现价来自行情通道；目标价为 15-30 日 ATR/动量/前高规则测算目标，不再用 EPS×PE 推算。</p>
        <div className="theme-grid">
          {grouped.map(({ theme, entries }) => (
            <div key={theme} className="theme-panel">
              <div className="theme-title">
                <strong>{theme}</strong>
                <span>{entries.length} 只</span>
              </div>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>代码</th>
                      <th>名称</th>
                      <th>全球链</th>
                      <th className="num">现价</th>
                      <th className="num" title="15-30 日 ATR/动量/前高规则测算目标">目标价</th>
                      <th className="num">上行</th>
                      <th className="num">目标置信度</th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map((entry) => {
                      const a = analystBySymbol.get(entry.symbol);
                      const hasTarget = a?.implied_target != null && Boolean(a?.target_price_source);
                      const upside = hasTarget ? a?.upside_pct : null;
                      return (
                        <tr key={entry.symbol}>
                          <td className="mono">{entry.symbol}</td>
                          <td>
                            <div className="stock-name">{entry.name}</div>
                            {entry.note && <div className="stock-note">{entry.note}</div>}
                          </td>
                          <td>{entry.global_supply ? <span className="pill good">是</span> : <span className="pill">否</span>}</td>
                          <td className="num">{num(a?.current_price)}</td>
                          <td className="num">{hasTarget ? num(a?.implied_target, 2) : "未覆盖"}</td>
                          <td className={`num ${upside == null ? "muted" : upside > 0 ? "pos" : "neg"}`}>
                            {pct(upside, 0, "未覆盖")}
                          </td>
                          <td className="num muted">
                            {a?.target_price_confidence != null
                              ? `${(a.target_price_confidence * 100).toFixed(0)}%`
                              : "未覆盖"}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section id="signals">
        <h2 className="subheading">明日信号快照</h2>
        <p className="muted">
          使用收盘后规则评分；信号日 {signalDate ?? "未生成"}。这是策略信号快照，具体换仓以 Dashboard 模拟仓为准。
        </p>
        <div className="theme-panel">
          <div className="theme-title">
            <strong>计划列表</strong>
            <span>{buys} 买入 · {sells} 卖出</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>代码</th>
                  <th>名称</th>
                  <th>主题</th>
                  <th>动作</th>
                  <th className="num">置信度</th>
                  <th className="num">仓位</th>
                  <th>理由</th>
                </tr>
              </thead>
              <tbody>
                {orderedSignals.map(({ entry, signal }) => (
                  <tr key={entry.symbol}>
                    <td className="mono">{entry.symbol}</td>
                    <td>{entry.name}</td>
                    <td className="muted">{entry.theme}</td>
                    <td><span className={`badge ${signal?.action ?? ""}`}>{actionLabel(signal?.action)}</span></td>
                    <td className="num">{signal ? `${(signal.confidence * 100).toFixed(0)}%` : "未评分"}</td>
                    <td className="num">{signal ? `${(signal.size * 100).toFixed(0)}%` : "未评分"}</td>
                    <td className="muted signal-reason">{signal?.rationale ?? "未生成信号"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="backtest">
        <h2 className="subheading">策略回测</h2>
        <p className="muted">
          {backtest.config.startDate} → {backtest.config.endDate} · 起始资金 {money(backtest.config.startCash)} · {decisionCadenceLabel}收盘决策 · 次日{priceFieldLabel(backtest.config.executionPrice === "next_open" ? "open" : undefined)}成交 · 最多 {backtest.config.maxPositions} 持仓 · 手续费 {backtest.config.feeBps}bps
        </p>
        {backtest.optimizedParams && (
          <p className="muted">
            当前固定参数：最短持仓 {backtest.optimizedParams.minHoldBars} 日 · 换手阈值 {backtest.optimizedParams.rebalanceThresholdPct}% · 买入分数 {backtest.optimizedParams.minScoreToBuy.toFixed(2)}
            {backtest.validationStats?.jan_2026 ? ` · 1月起算验证夏普 ${backtest.validationStats.jan_2026.sharpe.toFixed(2)}` : ""}
          </p>
        )}
        {meetsSharpeTarget ? (
          <div className="status-strip good">
            Sharpe 目标已达标：主窗口夏普 {backtest.stats.sharpe.toFixed(2)} / 目标 {sharpeTarget.toFixed(1)}。
          </div>
        ) : (
          <div className="warning-strip">
            Sharpe 目标未达标：当前主窗口夏普 {backtest.stats.sharpe.toFixed(2)} / 目标 {sharpeTarget.toFixed(1)}；不要把它包装成已验证有效策略。
          </div>
        )}
        {meetsSharpeTarget && (backtest.optimizationWarnings?.length ?? 0) > 0 && (
          <div className="warning-strip">
            夏普达标但存在约束提示：{backtest.optimizationWarnings?.join("、")}
          </div>
        )}
        <div className="summary-grid">
          <Metric label="总收益" value={pct(backtest.stats.totalReturnPct, 1)} sub="全程" tone={backtest.stats.totalReturnPct >= 0 ? "pos" : "neg"} />
          <Metric label="年化(CAGR)" value={pct(backtest.stats.cagrPct, 1)} sub="复合年化" tone={backtest.stats.cagrPct >= 0 ? "pos" : "neg"} />
          <Metric label="最大回撤" value={pct(backtest.stats.maxDrawdownPct, 1)} sub="峰谷" tone="neg" />
          <Metric label="夏普" value={num(backtest.stats.sharpe, 2)} sub={meetsSharpeTarget ? `已达标 ${sharpeTarget}` : `未达标 ${sharpeTarget}`} tone={meetsSharpeTarget ? "pos" : "neg"} />
          <Metric label="交易次数" value={`${backtest.stats.trades}`} sub="含减仓" />
          <Metric label="胜率" value={pct(backtest.stats.winRatePct, 1, "未覆盖")} sub="卖出/减仓实现" tone={(backtest.stats.winRatePct ?? 0) >= 50 ? "pos" : "neg"} />
          <Metric label="换手" value={pct(backtest.stats.turnoverPct, 0, "未覆盖")} sub="成交额/平均权益" />
        </div>
        <div className="theme-panel" style={{ marginTop: 14 }}>
          <div className="theme-title">
            <strong>近期交易</strong>
            <span>最新 40 笔</span>
          </div>
          <div className="table-wrap compact-table">
            <table>
              <thead>
                <tr>
                  <th>决策日</th>
                  <th>成交日</th>
                  <th>方向</th>
                  <th>代码</th>
                  <th>价型</th>
                  <th className="num">股数</th>
                  <th className="num">价格</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody>
                {recentTrades.map((trade, index) => (
                  <tr key={`${trade.date}-${trade.symbol}-${trade.side}-${index}`}>
                    <td className="mono">{trade.decisionDate ?? trade.date}</td>
                    <td className="mono">{trade.tradeDate ?? trade.date}</td>
                    <td><span className={`badge ${trade.side}`}>{sideLabel(trade.side)}</span></td>
                    <td className="mono">{trade.symbol}</td>
                    <td>{priceFieldLabel(trade.priceField)}</td>
                    <td className="num">{trade.shares.toLocaleString("en-US")}</td>
                    <td className="num">{num(trade.price)}</td>
                    <td className="muted signal-reason">{trade.reason ?? "-"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </div>
  );
}

function Metric({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub: string;
  tone?: "pos" | "neg";
}) {
  return (
    <div className="metric">
      <span className="label">{label}</span>
      <strong className={tone}>{value}</strong>
      <span>{sub}</span>
    </div>
  );
}
