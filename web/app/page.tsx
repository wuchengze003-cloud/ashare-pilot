import Link from "next/link";
import {
  isProductionSignalSnapshotStale,
  readProductionGate,
  readProductionSignals,
} from "@/lib/productionGate";
import {
  candidateGateLabel,
  productionCandidateName,
  productionCandidateStatus,
  productionFamilyName,
  productionReasonLabel,
} from "@/lib/productionPresentation";
import { readRuntimeJson } from "@/lib/runtimeData";
import { loadTradingConstraints } from "@/lib/tradingConstraints";
import { activeEntriesAsOf, readUniverse } from "@/lib/universe";
import {
  dailyCloseReceiptMatchesProduction,
  type DailyCloseProductionReceipt,
} from "@/lib/dailyClose";

export const dynamic = "force-dynamic";

interface UniverseEntry {
  symbol: string;
  name: string;
  theme: string;
  global_supply?: boolean;
}

interface UniverseSnapshot {
  updated_at: string;
  entries: UniverseEntry[];
}

interface RuntimeManifest {
  data_date?: string;
  latest_complete_date?: string;
}

interface DailyCloseHealth {
  generated_at: string;
  expected_market_date: string;
  latest_market_date?: string;
  status: "passed" | "failed" | "stale-or-no-session";
  production?: DailyCloseProductionReceipt;
}

function pct(value: number | null | undefined, digits = 2) {
  if (value == null || Number.isNaN(value)) return "暂无";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

function number(value: number | null | undefined, digits = 2) {
  return value == null || Number.isNaN(value) ? "暂无" : value.toFixed(digits);
}

function plainPct(value: number | null | undefined, digits = 1) {
  if (value == null || Number.isNaN(value)) return "暂无";
  return `${value.toFixed(digits)}%`;
}

export default function Home() {
  const universe = readUniverse() as UniverseSnapshot;
  const constraints = loadTradingConstraints();
  const gate = readProductionGate();
  const productionSignals = readProductionSignals();
  const manifest = readRuntimeJson<RuntimeManifest>("manifest.json");
  const health = readRuntimeJson<DailyCloseHealth>("daily-close-health.json");
  const closeHealthCurrent = Boolean(
    health &&
    health.status === "passed" &&
    dailyCloseReceiptMatchesProduction(
      health.production,
      gate,
      productionSignals,
    ),
  );
  const today = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  const dataDate =
    productionSignals?.latest_complete_date ??
    manifest?.latest_complete_date ??
    manifest?.data_date ??
    "暂无";
  const universeAsOf = /^\d{4}-\d{2}-\d{2}$/.test(dataDate) ? dataDate : today;
  const activeEntries = activeEntriesAsOf(universe.entries, universeAsOf);
  const themes = new Set(activeEntries.map((entry) => entry.theme)).size;
  const isActive = gate.status === "active";
  const minuteCoverage =
    gate.minute_coverage_pct == null
      ? "暂无"
      : `${gate.minute_coverage_pct.toFixed(2)}%`;
  const minuteRequired = gate.candidates.some(
    (candidate) => candidate.signal_frequency === "1d+5min",
  );
  const signalsStale =
    gate.status === "active" &&
    (!productionSignals || isProductionSignalSnapshotStale(productionSignals));
  const currentSignals =
    isActive &&
    !signalsStale &&
    productionSignals?.status === "active" &&
    productionSignals.champion_id === gate.champion_id
      ? productionSignals.signals
      : [];
  const openingSignals = currentSignals.filter(
    (signal) => signal.action === "buy" && signal.size > 0,
  );
  const grossBuyWeight = openingSignals.reduce(
    (sum, signal) => sum + signal.size,
    0,
  );
  const nameMap = new Map(activeEntries.map((entry) => [entry.symbol, entry.name]));

  return (
    <main className="container">
      <header className="page-header">
        <div>
          <div className="eyebrow">生产信号控制台</div>
          <h1>A股量化信号台</h1>
          <p>
            只展示通过固定研究合同、完整成本和交易约束、样本外验证及生产准入门禁的确定性信号。
            研究候选、旧版诊断和生产指令严格分离。
          </p>
          <p className="muted">
            最新完整交易日：{dataDate} · 股票池 {activeEntries.length} 只 · {themes} 个分组
          </p>
        </div>
        <nav className="header-actions" aria-label="页面导航">
          <Link href="/dashboard" className="button secondary">策略验收</Link>
          <Link href="/ops" className="button secondary">运维状态</Link>
        </nav>
      </header>

      <section
        className={`production-status ${isActive && !signalsStale ? "active" : "cash-only"}`}
        aria-labelledby="production-status-title"
      >
        <div>
          <div className="eyebrow">当前生产决策</div>
          <h2 id="production-status-title">
            {isActive
              ? signalsStale
                ? "信号数据陈旧，暂停执行"
                : "生产策略运行中"
              : "保持现金，不开新仓"}
          </h2>
          <p>
            {signalsStale
              ? "生产信号快照超过新鲜度窗口，系统按 fail-closed 处理：不展示可执行买点，等待收盘链路重新发布。"
              : gate.message}
          </p>
        </div>
        <div className={`production-state ${isActive && !signalsStale ? "active" : "cash-only"}`}>
          {isActive ? (signalsStale ? "数据陈旧" : "运行中") : "仅现金"}
        </div>
      </section>

      <div className="summary-grid" id="signals">
        <Metric
          label="唯一上线策略"
          value={gate.champion_id ? productionCandidateName(gate.champion_id) : "无"}
          sub={isActive ? gate.champion_id ?? "" : "未通过门禁不发布买点"}
          tone={isActive ? "pos" : "neg"}
        />
        <Metric
          label="开仓信号"
          value={`${openingSignals.length}`}
          sub={`计划总仓位 ${(grossBuyWeight * 100).toFixed(0)}%`}
          tone={openingSignals.length > 0 ? "pos" : undefined}
        />
        <Metric
          label={minuteRequired ? "分钟需求覆盖" : "本轮执行频率"}
          value={minuteRequired ? minuteCoverage : "日频"}
          sub={
            minuteRequired
              ? gate.required_symbol_days == null
                ? "分钟覆盖审计尚未完成"
                : `${gate.available_symbol_days ?? 0}/${gate.required_symbol_days} 个预注册标的日`
              : "三条候选均不依赖分钟数据"
          }
          tone={
            minuteRequired
              ? gate.minute_coverage_pct === 100 ? "pos" : "neg"
              : "pos"
          }
        />
        <Metric
          label="收盘链路"
          value={closeHealthCurrent ? "通过" : "待重验"}
          sub={
            closeHealthCurrent
              ? health?.latest_market_date ?? dataDate
              : "当前生产快照尚无完整收盘回执"
          }
          tone={closeHealthCurrent ? "pos" : undefined}
        />
      </div>

      {!isActive && (
        <section className="decision-reasons" aria-labelledby="decision-reasons-title">
          <h2 id="decision-reasons-title">未上线原因</h2>
          <ul>
            {gate.reason_codes.map((code) => (
              <li key={code}>
                <strong>{productionReasonLabel(code)}</strong>
              </li>
            ))}
          </ul>
        </section>
      )}

      {isActive && (
        <section style={{ marginTop: 24 }}>
          <div className="section-heading">
            <div>
              <div className="eyebrow">可执行信号</div>
              <h2>当前生产信号</h2>
            </div>
            <span className="pill good">
              {productionSignals?.signal_date ?? dataDate} 确定性快照
            </span>
          </div>
          <div className="theme-panel">
            <div className="table-wrap compact-table">
              <table>
                <thead>
                  <tr>
                    <th>代码</th>
                    <th>名称</th>
                    <th>动作</th>
                    <th className="num">目标仓位</th>
                    <th className="num">置信度</th>
                    <th>依据</th>
                  </tr>
                </thead>
                <tbody>
                  {currentSignals.map((signal) => (
                    <tr key={signal.symbol}>
                      <td className="mono">{signal.symbol}</td>
                      <td>{nameMap.get(signal.symbol) ?? "名称未收录"}</td>
                      <td>
                        <span className={`badge ${signal.action}`}>
                          {signal.action === "buy" ? "买入" : signal.action === "sell" ? "卖出" : "持有"}
                        </span>
                      </td>
                      <td className="num">{(signal.size * 100).toFixed(1)}%</td>
                      <td className="num">{(signal.confidence * 100).toFixed(0)}%</td>
                      <td className="muted signal-reason">{signal.rationale}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      <section className="strategy-panel" aria-labelledby="race-title">
        <div className="strategy-panel-head">
          <div>
            <div className="eyebrow">预注册策略赛马</div>
            <h2 id="race-title">三策略赛马</h2>
          </div>
          <span className="pill">研究候选，不等于生产信号</span>
        </div>
        <div className="strategy-grid">
          {gate.candidates.map((candidate) => (
            <article className="strategy-card" key={candidate.candidate_id}>
              <div className="strategy-card-head">
                <span>
                  {productionCandidateName(candidate.candidate_id)}
                </span>
                <span className={`pill ${candidate.candidate_id === gate.champion_id ? "good" : ""}`}>
                  {productionCandidateStatus(candidate, gate.champion_id)}
                </span>
              </div>
              <strong>{productionFamilyName(candidate.family)}</strong>
              <dl className="candidate-metrics">
                <div><dt>OOS 年化</dt><dd>{pct(candidate.oos_annualized_return_pct)}</dd></div>
                <div><dt>OOS Sharpe</dt><dd>{number(candidate.oos_sharpe)}</dd></div>
                <div><dt>冻结期 Sharpe</dt><dd>{number(candidate.frozen_sharpe)}</dd></div>
                <div><dt>OOS 最大回撤</dt><dd>{pct(candidate.oos_max_drawdown_pct)}</dd></div>
                <div><dt>上涨捕获</dt><dd>{pct(candidate.oos_upside_capture_pct)}</dd></div>
                <div><dt>Bootstrap 置信度</dt><dd>{plainPct(candidate.bootstrap_probability_pct, 1)}</dd></div>
              </dl>
              {candidate.failed_gate_codes.length > 0 && (
                <div className="candidate-failures">
                  <span>未通过：</span>
                  {candidate.failed_gate_codes.map((code) => (
                    <span className="badge sell" key={code}>{candidateGateLabel(code)}</span>
                  ))}
                </div>
              )}
            </article>
          ))}
          {gate.candidates.length === 0 && (
            <div className="muted">赛马报告尚未生成或未通过结构校验。</div>
          )}
        </div>
      </section>

      <section style={{ marginTop: 24 }}>
        <h2 className="subheading">生产约束</h2>
        <div className="constraint-grid">
          <div><span>初始资金</span><strong>¥{constraints.initialCapitalYuan.toLocaleString("en-US")}</strong></div>
          <div><span>回撤晋级上限</span><strong>{constraints.maxDrawdownPct}%</strong></div>
          <div><span>单股上限</span><strong>{constraints.maxSinglePositionPct}%</strong></div>
          <div><span>最大持仓</span><strong>{constraints.maxPositions} 只</strong></div>
          <div><span>交易制度</span><strong>{constraints.tPlusOne ? "T+1" : "未配置"}</strong></div>
          <div><span>系统支持频率</span><strong>{constraints.supportedSignalFrequencies.join(" + ")}</strong></div>
        </div>
      </section>

      <section style={{ marginTop: 28 }}>
        <h2 className="subheading">风险边界</h2>
        <div className="risk-copy">
          <p>
            回测与样本外结果用于约束策略上线，不构成收益承诺。15% 是晋级与降险上限，
            不是未来回撤保证；数据缺失、执行不可复现或冠军证据不一致时，系统自动保持现金。
          </p>
          <p>
            本产品发布确定性策略信号，不连接券商下单。使用者仍需自行核对账户、价格、可卖数量及市场状态。
          </p>
        </div>
      </section>
    </main>
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
