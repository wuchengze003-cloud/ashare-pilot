import Link from "next/link";
import {
  readProductionGate,
  readProductionSignals,
} from "@/lib/productionGate";
import {
  candidateGateLabel,
  productionCandidateName,
  productionCandidateStatus,
  productionFamilyName,
} from "@/lib/productionPresentation";
import { loadTradingConstraints } from "@/lib/tradingConstraints";

export const dynamic = "force-dynamic";

function signedPct(value: number | null | undefined, digits = 2) {
  if (value == null || Number.isNaN(value)) return "暂无";
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

function plainPct(value: number | null | undefined, digits = 1) {
  if (value == null || Number.isNaN(value)) return "暂无";
  return `${value.toFixed(digits)}%`;
}

function number(value: number | null | undefined, digits = 2) {
  return value == null || Number.isNaN(value) ? "暂无" : value.toFixed(digits);
}

export default function DashboardPage() {
  const gate = readProductionGate();
  const signals = readProductionSignals();
  const constraints = loadTradingConstraints();
  const isActive = gate.status === "active";
  const minuteRequired = gate.candidates.some(
    (candidate) => candidate.signal_frequency === "1d+5min",
  );

  return (
    <main className="container">
      <Link href="/" className="back-link">返回信号台</Link>
      <header className="page-header compact">
        <div>
          <div className="eyebrow">策略生产验收</div>
          <h1>三策略赛马结果</h1>
          <p>
            三条收益来源不同的候选使用同一份固定数据、完整交易成本和 A 股交易约束，
            经过预注册验证、样本外、冻结期及稳健性门禁后，只允许一条策略进入生产。
          </p>
        </div>
        <nav className="header-actions" aria-label="页面导航">
          <Link href="/" className="button secondary">生产信号</Link>
          <Link href="/ops" className="button secondary">运维状态</Link>
        </nav>
      </header>

      <section
        className={`production-status ${isActive ? "active" : "cash-only"}`}
        aria-labelledby="acceptance-status-title"
      >
        <div>
          <div className="eyebrow">最终准入结论</div>
          <h2 id="acceptance-status-title">
            {isActive
              ? `${productionCandidateName(gate.champion_id ?? "")} 已成为唯一生产策略`
              : "本轮无冠军，生产端保持现金"}
          </h2>
          <p>{gate.message}</p>
        </div>
        <div className={`production-state ${isActive ? "active" : "cash-only"}`}>
          {isActive ? "已上线" : "未晋级"}
        </div>
      </section>

      <div className="summary-grid">
        <Metric
          label="候选数量"
          value={`${gate.candidates.length}`}
          sub="固定三条并行评估"
        />
        <Metric
          label="通过全部门禁"
          value={`${gate.candidates.filter((candidate) => candidate.daily_gates_passed).length}`}
          sub={isActive ? "仅冠军可发布信号" : "未通过时不发布买点"}
          tone={isActive ? "pos" : "neg"}
        />
        <Metric
          label="评估数据截止"
          value={gate.panel_end ?? "暂无"}
          sub={`${gate.complete_daily_trading_days ?? 0} 个完整交易日`}
        />
        <Metric
          label="执行数据"
          value={minuteRequired ? "日线 + 5分钟" : "日线"}
          sub={minuteRequired ? "分钟覆盖必须完整" : "本轮不依赖分钟覆盖"}
          tone={!minuteRequired ? "pos" : undefined}
        />
      </div>

      <section className="section-heading">
        <div>
          <div className="eyebrow">样本外对比</div>
          <h2>候选表现与准入结论</h2>
        </div>
        <span className="pill">100 万元初始资金 · 成本后结果</span>
      </section>

      <div className="theme-panel" style={{ marginTop: 12 }}>
        <div className="table-wrap compact-table">
          <table className="race-table">
            <thead>
              <tr>
                <th>候选</th>
                <th>收益来源</th>
                <th className="num">OOS 年化</th>
                <th className="num">OOS Sharpe</th>
                <th className="num">OOS 回撤</th>
                <th className="num">冻结期收益</th>
                <th className="num">冻结期 Sharpe</th>
                <th className="num">上涨捕获</th>
                <th className="num">正收益折</th>
                <th className="num">Bootstrap</th>
                <th className="num">交易数</th>
                <th>结论</th>
              </tr>
            </thead>
            <tbody>
              {gate.candidates.map((candidate) => (
                <tr key={candidate.candidate_id}>
                  <td>
                    <strong>{productionCandidateName(candidate.candidate_id)}</strong>
                    <div className="mono" style={{ marginTop: 3, fontSize: 11 }}>
                      {candidate.candidate_id}
                    </div>
                  </td>
                  <td>{productionFamilyName(candidate.family)}</td>
                  <td className={`num ${(candidate.oos_annualized_return_pct ?? 0) >= 0 ? "pos" : "neg"}`}>
                    {signedPct(candidate.oos_annualized_return_pct)}
                  </td>
                  <td className="num">{number(candidate.oos_sharpe)}</td>
                  <td className="num neg">{signedPct(candidate.oos_max_drawdown_pct)}</td>
                  <td className={`num ${(candidate.frozen_total_return_pct ?? 0) >= 0 ? "pos" : "neg"}`}>
                    {signedPct(candidate.frozen_total_return_pct)}
                  </td>
                  <td className="num">{number(candidate.frozen_sharpe)}</td>
                  <td className="num">{plainPct(candidate.oos_upside_capture_pct)}</td>
                  <td className="num">{plainPct(candidate.positive_oos_fold_share_pct, 0)}</td>
                  <td className="num">{plainPct(candidate.bootstrap_probability_pct, 1)}</td>
                  <td className="num">{candidate.oos_closed_trades ?? "暂无"}</td>
                  <td>
                    <span className={`pill ${candidate.daily_gates_passed ? "good" : "bad"}`}>
                      {productionCandidateStatus(candidate, gate.champion_id)}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <section className="strategy-panel" aria-labelledby="failed-gates-title">
        <div className="strategy-panel-head">
          <div>
            <div className="eyebrow">未通过项目</div>
            <h2 id="failed-gates-title">门禁失败明细</h2>
          </div>
          <span className="pill">不事后改口径，不降低阈值</span>
        </div>
        <div className="strategy-grid">
          {gate.candidates.map((candidate) => (
            <article className="strategy-card" key={candidate.candidate_id}>
              <div className="strategy-card-head">
                <span>{productionCandidateName(candidate.candidate_id)}</span>
                <span className={`pill ${candidate.failed_gate_codes.length === 0 ? "good" : "bad"}`}>
                  {candidate.failed_gate_codes.length === 0
                    ? "全部通过"
                    : `${candidate.failed_gate_codes.length} 项未通过`}
                </span>
              </div>
              <strong>{productionFamilyName(candidate.family)}</strong>
              <div className="candidate-failures">
                {candidate.failed_gate_codes.length === 0 ? (
                  <span className="muted">没有失败项</span>
                ) : candidate.failed_gate_codes.map((code) => (
                  <span className="badge sell" key={code}>{candidateGateLabel(code)}</span>
                ))}
              </div>
            </article>
          ))}
        </div>
      </section>

      <section style={{ marginTop: 24 }}>
        <h2 className="subheading">研究与执行边界</h2>
        <div className="constraint-grid">
          <div><span>初始资金</span><strong>¥{constraints.initialCapitalYuan.toLocaleString("en-US")}</strong></div>
          <div><span>最大回撤门禁</span><strong>{constraints.maxDrawdownPct}%</strong></div>
          <div><span>信号执行</span><strong>系统生成，用户确认</strong></div>
          <div><span>研究合同</span><strong>{gate.contract_version ?? "暂无"}</strong></div>
          <div><span>特征版本</span><strong>{gate.feature_version ?? "暂无"}</strong></div>
          <div><span>最新信号日</span><strong>{signals?.signal_date ?? "暂无"}</strong></div>
        </div>
        <div className="risk-copy">
          <p>
            当前数据包固定使用现有 Tushare 日线和本地分钟数据，不依赖新增付费数据。
            日频候选可独立完成准入；只有明确使用 5 分钟信号的候选才受分钟覆盖门禁约束。
          </p>
          <p>
            2025 年后的最终样本只用于一次性验收，不用于事后调参。
            本轮无策略通过全部门禁，因此生产系统保持现金，而不是强行选择表现最接近的一条。
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
