import Link from "next/link";
import { readRuntimeJson, readStrategyJson } from "@/lib/runtimeData";
import { activeEntriesAsOf, readUniverse } from "@/lib/universe";
import { STRATEGIES } from "@/lib/strategyRegistry";
import type { DashboardData } from "./dashboard/types";

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

interface StrategyComparisonEntry {
  id: string;
  name: string;
  totalReturnPct: number;
  cagrPct: number;
  maxDrawdownPct: number;
  sharpe: number;
  trades: number;
  winRatePct?: number;
}

interface StrategyComparison {
  generated_at: string;
  data_date: string;
  strategies: StrategyComparisonEntry[];
}

interface SignalItem {
  symbol: string;
  action: "buy" | "hold" | "sell";
  confidence: number;
  size: number;
  rationale: string;
}

interface SignalsSnapshot {
  generated_at: string;
  signal_date: string;
  signals: SignalItem[];
}

interface MetaSnapshot {
  generated_at: string;
  universe_count: number;
}

interface RuntimeManifest {
  generated_at?: string;
  git_sha?: string;
  universe_sha?: string;
  data_date?: string;
  latest_complete_date?: string;
  snapshot_basis?: string;
  strategies?: Array<{ id: string; name: string }>;
}

interface DailyCloseHealth {
  generated_at: string;
  expected_market_date: string;
  latest_market_date?: string;
  status: "passed" | "failed" | "stale-or-no-session";
  steps?: Array<{ name: string; status: string; detail?: string }>;
}

function pct(v: number, digits = 2) {
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

export default function Home() {
  const universe = readUniverse() as UniverseSnapshot;
  const today = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  const activeEntries = activeEntriesAsOf(universe.entries, today);
  const comparison = readRuntimeJson<StrategyComparison>("strategy-comparison.json");
  const meta = readRuntimeJson<MetaSnapshot>("meta.json");
  const manifest = readRuntimeJson<RuntimeManifest>("manifest.json");
  const health = readRuntimeJson<DailyCloseHealth>("daily-close-health.json");

  // Load default strategy signals for public display
  const defaultSignals = readStrategyJson<SignalsSnapshot>("momentum-v1", "signals.json");
  // Load default strategy backtest for market status
  const defaultBacktest = readStrategyJson<DashboardData>("momentum-v1", "backtest.json");

  const themes = [...new Set(activeEntries.map((e) => e.theme))].sort();
  const globalCount = activeEntries.filter((e) => e.global_supply).length;
  const dataDate = manifest?.data_date ?? manifest?.latest_complete_date ?? comparison?.data_date ?? "暂无";
  const latestBar = defaultBacktest?.equityCurve.at(-1);
  const latestEquity = latestBar?.equity ?? 0;
  const latestCash = latestBar?.cash ?? 0;
  const cashWeight = latestEquity > 0 ? latestCash / latestEquity : 0;
  const holdingsCount = defaultBacktest ? Object.keys(defaultBacktest.latestHoldings).length : 0;

  // Top 5 buy signals for public display
  const topBuys = (defaultSignals?.signals ?? [])
    .filter((s) => s.action === "buy" && s.size > 0)
    .sort((a, b) => b.confidence - a.confidence)
    .slice(0, 5);

  const nameMap = new Map(activeEntries.map((e) => [e.symbol, e.name]));
  const themeMap = new Map(activeEntries.map((e) => [e.symbol, e.theme]));

  return (
    <div className="container">
      <header className="page-header">
        <div>
          <div className="eyebrow">量化策略 · 多因子回测 · 实时信号</div>
          <h1>A股量化策略站</h1>
          <p>
            三策略并行量化系统：右侧动量 (Momentum-V1)、潮汐 (Tide)、棱镜 (Prism)。
            共享 AI 算力产业链股票池，独立持仓、独立权益曲线、独立交易明细。
            每日收盘决策，次日开盘执行。
          </p>
          <p className="muted">
            数据截止：{dataDate} · 股票池 {activeEntries.length} 只 · {themes.length} 个子主题 · 股票池更新：{universe.updated_at}
          </p>
        </div>
        <nav className="header-actions" aria-label="页面导航">
          <Link href="/dashboard" className="button secondary" data-umami-event="nav-dashboard">策略对比</Link>
          {STRATEGIES.map((s) => (
            <Link key={s.id} href={`/dashboard/${s.id}`} className="button secondary" data-umami-event={`nav-${s.id}`}>
              {s.name}
            </Link>
          ))}
        </nav>
      </header>

      {/* Summary metrics */}
      <div className="summary-grid">
        <Metric label="股票池" value={`${activeEntries.length}`} sub={`${themes.length} 个子主题`} />
        <Metric label="全球供应链" value={`${globalCount}`} sub={`${Math.round((globalCount / activeEntries.length) * 100)}% 覆盖`} />
        <Metric label="策略数量" value={`${STRATEGIES.length}`} sub="同步运行观察" />
        <Metric label="最新数据" value={dataDate} sub={health?.status === "passed" ? "daily-close 通过" : health?.status ?? "未运行"} />
      </div>

      {/* Strategy cards */}
      <section className="strategy-panel" aria-labelledby="strategy-title">
        <div className="strategy-panel-head">
          <div>
            <div className="eyebrow">Strategy Engine</div>
            <h2 id="strategy-title">策略矩阵</h2>
          </div>
          <span className="pill good">每日收盘决策 · 次日开盘成交</span>
        </div>
        <div className="strategy-grid">
          {STRATEGIES.map((s) => {
            const stratData = readStrategyJson<DashboardData>(s.id, "backtest.json");
            const stats = stratData?.stats;
            return (
              <Link key={s.id} href={`/dashboard/${s.id}`} style={{ textDecoration: "none", color: "inherit" }}>
                <div className="strategy-card" style={{ cursor: "pointer" }}>
                  <span className="strategy-label">{s.name} <small style={{ opacity: 0.6 }}>{s.codename}</small></span>
                  <strong>{s.factors.join(" · ")}</strong>
                  <p>{s.description}</p>
                  {stats && (
                    <div style={{ display: "flex", gap: 12, flexWrap: "wrap", fontSize: 13, marginTop: 8 }}>
                      <span>总收益 <strong className={stats.totalReturnPct >= 0 ? "pos" : "neg"}>{pct(stats.totalReturnPct)}</strong></span>
                      <span>夏普 <strong>{stats.sharpe.toFixed(2)}</strong></span>
                      <span>最大回撤 <strong className="neg">{pct(stats.maxDrawdownPct)}</strong></span>
                    </div>
                  )}
                </div>
              </Link>
            );
          })}
          <div>
            <span className="strategy-label">风控约束</span>
            <strong>不追单日 &gt;5% · 不追 5 日 &gt;18%</strong>
            <p>初始资金 100 万 · 最大回撤 20% · 单只 25% · 单主题 40% · 最短持仓 5 日</p>
          </div>
        </div>
      </section>

      {/* Strategy comparison table */}
      {comparison && (
        <section style={{ marginTop: 24 }}>
          <h2 className="subheading">策略对比核心指标</h2>
          <div className="card" style={{ marginTop: 8 }}>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>策略</th>
                    <th className="num">总收益</th>
                    <th className="num">年化</th>
                    <th className="num">最大回撤</th>
                    <th className="num">夏普</th>
                    <th className="num">交易次数</th>
                    <th className="num">胜率</th>
                    <th>详情</th>
                  </tr>
                </thead>
                <tbody>
                  {comparison.strategies.map((s) => (
                    <tr key={s.id}>
                      <td><strong>{s.name}</strong></td>
                      <td className={`num ${s.totalReturnPct >= 0 ? "pos" : "neg"}`}>{pct(s.totalReturnPct)}</td>
                      <td className={`num ${s.cagrPct >= 0 ? "pos" : "neg"}`}>{pct(s.cagrPct)}</td>
                      <td className="num neg">{pct(s.maxDrawdownPct)}</td>
                      <td className="num">{s.sharpe.toFixed(2)}</td>
                      <td className="num">{s.trades}</td>
                      <td className="num">{s.winRatePct != null ? pct(s.winRatePct) : "暂无"}</td>
                      <td><Link href={`/dashboard/${s.id}`} style={{ fontSize: 13 }}>查看 →</Link></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="muted" style={{ padding: "8px 16px", fontSize: 12 }}>
              对比数据生成于 {new Date(comparison.generated_at).toLocaleString("zh-CN")}
            </div>
          </div>
        </section>
      )}

      {/* Current market status */}
      {defaultBacktest && (
        <section style={{ marginTop: 24 }}>
          <h2 className="subheading">当前市场状态</h2>
          <div className="summary-grid">
            <Metric label="总权益" value={`¥${Math.round(latestEquity).toLocaleString("en-US")}`} sub={defaultBacktest.latestDate} />
            <Metric label="持仓数量" value={`${holdingsCount} 只`} sub={`现金 ${pct(cashWeight * 100, 1)}`} />
            <Metric label="沪深300基准" value={pct(((latestEquity / defaultBacktest.config.startCash) - 1) * 100, 1)} sub={defaultBacktest.config.endDate} />
            <Metric label="策略状态" value={health?.status === "passed" ? "运行正常" : "待更新"} sub={health?.expected_market_date ?? dataDate} />
          </div>
        </section>
      )}

      {/* Recent public signals */}
      {topBuys.length > 0 && (
        <section style={{ marginTop: 24 }}>
          <h2 className="subheading">最新买入信号</h2>
          <p className="muted">
            信号日 {defaultSignals?.signal_date ?? "暂无"} · 展示 Momentum-V1 策略前 5 个买入信号
          </p>
          <div className="theme-panel">
            <div className="table-wrap compact-table">
              <table>
                <thead>
                  <tr>
                    <th>代码</th>
                    <th>名称</th>
                    <th>主题</th>
                    <th className="num">置信度</th>
                    <th className="num">目标仓位</th>
                    <th>理由</th>
                  </tr>
                </thead>
                <tbody>
                  {topBuys.map((signal) => (
                    <tr key={signal.symbol}>
                      <td className="mono">{signal.symbol}</td>
                      <td>{nameMap.get(signal.symbol) ?? "名称未收录"}</td>
                      <td className="muted">{themeMap.get(signal.symbol) ?? "主题未收录"}</td>
                      <td className="num">{(signal.confidence * 100).toFixed(0)}%</td>
                      <td className="num">{(signal.size * 100).toFixed(0)}%</td>
                      <td className="muted signal-reason">{signal.rationale}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          <p style={{ marginTop: 8 }}>
            <Link href="/dashboard/momentum-v1" className="button secondary" data-umami-event="nav-full-signals">
              查看完整信号 →
            </Link>
          </p>
        </section>
      )}

      {/* Methodology and risk disclosure */}
      <section style={{ marginTop: 32 }}>
        <h2 className="subheading">方法论与风险说明</h2>
        <div className="card" style={{ padding: 20 }}>
          <h3 style={{ marginTop: 0 }}>策略方法论</h3>
          <ul style={{ color: "var(--muted)", lineHeight: 1.8, fontSize: 14 }}>
            <li><strong>Momentum-V1（右侧动量）</strong>：价格动量 35% + 主题强度 30% + 量能确认 20% + 趋势形态 15%。捕捉右侧趋势启动，通过多因子评分筛选高确定性标的。</li>
            <li><strong>Tide（潮汐）</strong>：资金流微观结构策略，通过量价关系推导机构资金进出节奏，捕捉主力建仓/出货周期。Tide-V2 接入 Tushare 真实资金流数据。</li>
            <li><strong>Prism（棱镜）</strong>：自适应多因子策略，基于市场状态检测动态旋转因子权重。趋势市追动量、震荡市做均值回归。Prism-V2 接入全市场 regime 数据。</li>
          </ul>
          <h3>执行机制</h3>
          <ul style={{ color: "var(--muted)", lineHeight: 1.8, fontSize: 14 }}>
            <li>每日收盘后运行评分，次日开盘价成交</li>
            <li>初始资金 100 万元，最大持仓 8 只</li>
            <li>单只标的最大仓位 25%，单主题最大 40%</li>
            <li>不追单日涨幅 &gt;5% 或 5 日涨幅 &gt;18% 的标的</li>
            <li>最短持仓 5 个交易日，硬退出信号不受此限制</li>
          </ul>
          <h3>风险提示</h3>
          <ul style={{ color: "var(--muted)", lineHeight: 1.8, fontSize: 14 }}>
            <li>本站所有数据仅供参考，不构成投资建议</li>
            <li>回测结果基于历史数据，不代表未来收益</li>
            <li>量化策略存在模型风险、数据风险和执行风险</li>
            <li>A股市场有波动风险，投资需谨慎</li>
            <li>本系统不管理真实资金，所有持仓均为模拟</li>
          </ul>
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
