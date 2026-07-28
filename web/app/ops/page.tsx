import Link from "next/link";
import fs from "node:fs";
import path from "node:path";
import { cookies, headers } from "next/headers";
import { readRuntimeJson, readStrategyJson } from "@/lib/runtimeData";
import { STRATEGIES } from "@/lib/strategyRegistry";
import type { DashboardData } from "../dashboard/types";

export const dynamic = "force-dynamic";

interface RuntimeManifest {
  generated_at?: string;
  git_sha?: string;
  universe_sha?: string;
  data_date?: string;
  latest_complete_date?: string;
  snapshot_basis?: string;
  strategies?: Array<{ id: string; name: string }>;
  cost_model?: string;
  file_hashes?: Record<string, string>;
}

interface DailyCloseHealth {
  generated_at: string;
  expected_market_date: string;
  latest_market_date?: string;
  status: "passed" | "failed" | "stale-or-no-session";
  steps?: Array<{ name: string; status: string; detail?: string; durationMs?: number }>;
  error?: string;
  local?: { pyserver_url: string; web_url: string };
  remote?: { base_url: string };
}

interface AgentRunResult {
  id: string;
  agent: string;
  mode: string;
  status: string;
  changed_paths?: string[];
  duration_seconds?: number;
  stdout?: string;
}

function pct(v: number, digits = 2) {
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

async function checkAuth(): Promise<boolean> {
  const token = process.env.OPS_TOKEN;
  if (!token) return true; // No token configured = open access (for dev)
  const cookieStore = await cookies();
  const cookieToken = cookieStore.get("ops_token")?.value;
  if (cookieToken === token) return true;
  const headerStore = await headers();
  const authHeader = headerStore.get("authorization");
  if (authHeader === `Bearer ${token}`) return true;
  return false;
}

export default async function OpsPage() {
  const authenticated = await checkAuth();

  if (!authenticated) {
    return (
      <div className="container">
        <header className="page-header compact">
          <div>
            <div className="eyebrow">Ops</div>
            <h1>运维面板</h1>
            <p>需要认证才能访问。请提供有效的 OPS_TOKEN。</p>
          </div>
        </header>
        <div className="card" style={{ borderColor: "var(--warn)" }}>
          <strong>未授权</strong>
          <p style={{ color: "var(--muted)" }}>
            此页面包含模型注册、数据源状态、晋级评估等运维信息。
            请联系管理员获取访问令牌。
          </p>
        </div>
      </div>
    );
  }

  const manifest = readRuntimeJson<RuntimeManifest>("manifest.json");
  const health = readRuntimeJson<DailyCloseHealth>("daily-close-health.json");

  // Read agent dispatch run results from ops/agents/runtime/
  const agentRunDir = path.resolve(process.cwd(), "..", "ops", "agents", "runtime");
  let agentRuns: AgentRunResult[] = [];
  try {
    if (fs.existsSync(agentRunDir)) {
      agentRuns = fs.readdirSync(agentRunDir)
        .filter((f) => f.endsWith(".json"))
        .sort()
        .reverse()
        .slice(0, 10)
        .map((f) => JSON.parse(fs.readFileSync(path.join(agentRunDir, f), "utf-8")) as AgentRunResult);
    }
  } catch {
    // Directory not accessible in this environment
  }

  // Load all strategy data for ops overview
  const strategyData = STRATEGIES.map((meta) => ({
    meta,
    data: readStrategyJson<DashboardData>(meta.id, "backtest.json"),
  }));

  return (
    <div className="container">
      <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 12 }}>
        <Link href="/" className="back-link">返回首页</Link>
        <span style={{ color: "var(--muted)" }}>/</span>
        <span style={{ fontSize: 14, color: "var(--muted)" }}>Ops</span>
      </div>

      <header className="page-header compact">
        <div>
          <div className="eyebrow">Ops</div>
          <h1>运维面板</h1>
          <p>模型注册、数据源状态、晋级评估、运行目录。</p>
        </div>
      </header>

      {/* Version info */}
      <h2 className="subheading">版本信息</h2>
      <div className="card" style={{ marginTop: 8 }}>
        {manifest ? (
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(250px, 1fr))", gap: 12, padding: 16 }}>
            <div>
              <span className="muted" style={{ fontSize: 12 }}>Commit SHA</span>
              <div className="mono" style={{ fontSize: 14 }}>{manifest.git_sha ?? "unknown"}</div>
            </div>
            <div>
              <span className="muted" style={{ fontSize: 12 }}>数据日期</span>
              <div style={{ fontSize: 14 }}>{manifest.data_date ?? manifest.latest_complete_date ?? "unknown"}</div>
            </div>
            <div>
              <span className="muted" style={{ fontSize: 12 }}>Universe SHA</span>
              <div className="mono" style={{ fontSize: 14 }}>{manifest.universe_sha ?? "unknown"}</div>
            </div>
            <div>
              <span className="muted" style={{ fontSize: 12 }}>快照类型</span>
              <div style={{ fontSize: 14 }}>{manifest.snapshot_basis ?? "unknown"}</div>
            </div>
            <div>
              <span className="muted" style={{ fontSize: 12 }}>生成时间</span>
              <div style={{ fontSize: 14 }}>{manifest.generated_at ? new Date(manifest.generated_at).toLocaleString("zh-CN") : "unknown"}</div>
            </div>
            <div>
              <span className="muted" style={{ fontSize: 12 }}>成本模型</span>
              <div style={{ fontSize: 14 }}>{manifest.cost_model ?? "unknown"}</div>
            </div>
          </div>
        ) : (
          <p className="muted" style={{ padding: 16 }}>manifest.json 未生成</p>
        )}
      </div>

      {/* Daily-close health */}
      <h2 className="subheading">Daily-Close 健康状态</h2>
      <div className="card" style={{ marginTop: 8 }}>
        {health ? (
          <>
            <div style={{ display: "flex", gap: 16, padding: "12px 16px", alignItems: "center" }}>
              <span className={`pill ${health.status === "passed" ? "good" : health.status === "stale-or-no-session" ? "" : "bad"}`}>
                {health.status}
              </span>
              <span>预期日期：{health.expected_market_date}</span>
              <span>最新行情：{health.latest_market_date ?? "unknown"}</span>
              <span>生成时间：{new Date(health.generated_at).toLocaleString("zh-CN")}</span>
            </div>
            {health.error && (
              <div className="warning-strip" style={{ margin: "0 16px" }}>
                {health.error}
              </div>
            )}
            {health.steps && health.steps.length > 0 && (
              <div className="table-wrap compact-table">
                <table>
                  <thead>
                    <tr>
                      <th>步骤</th>
                      <th>状态</th>
                      <th className="num">耗时</th>
                      <th>详情</th>
                    </tr>
                  </thead>
                  <tbody>
                    {health.steps.map((step, i) => (
                      <tr key={i}>
                        <td>{step.name}</td>
                        <td>
                          <span className={`pill ${step.status === "passed" ? "good" : step.status === "skipped" ? "" : "bad"}`}>
                            {step.status}
                          </span>
                        </td>
                        <td className="num">{step.durationMs != null ? `${(step.durationMs / 1000).toFixed(1)}s` : "-"}</td>
                        <td className="muted" style={{ fontSize: 12, maxWidth: 400 }}>{step.detail ?? "-"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        ) : (
          <p className="muted" style={{ padding: 16 }}>daily-close-health.json 未生成</p>
        )}
      </div>

      {/* Model registration & promotion status */}
      <h2 className="subheading">模型注册与晋级状态</h2>
      <div className="card" style={{ marginTop: 8 }}>
        {strategyData.map(({ meta, data }) => {
          const research = data?.researchStatus;
          const assessment = research?.promotion_assessments?.at(-1);
          return (
            <div key={meta.id} style={{ padding: "12px 16px", borderBottom: "1px solid var(--border, #222)" }}>
              <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 8 }}>
                <strong>{meta.name}</strong>
                <span className="mono muted">{meta.codename}</span>
                <span className={`pill ${research?.production_strategy === "ml-champion" ? "good" : ""}`}>
                  {research?.production_strategy ?? "v1-rule"}
                </span>
                {research?.activation_pending && (
                  <span className="pill">待激活：{research.activation_pending}</span>
                )}
              </div>
              {assessment ? (
                <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13, color: "var(--muted)" }}>
                  <span>状态：{assessment.status ?? "unknown"}</span>
                  <span>影子天数：{assessment.metrics?.shadow_trading_days ?? 0}/60</span>
                  <span>完成交易：{assessment.metrics?.closed_trades ?? 0}/20</span>
                  <span>OOS Sharpe：{assessment.metrics?.oos_sharpe?.toFixed(2) ?? "暂无"}</span>
                  <span>最大回撤：{assessment.metrics?.max_drawdown_pct != null ? pct(assessment.metrics.max_drawdown_pct) : "暂无"}</span>
                  {assessment.failures && assessment.failures.length > 0 ? (
                    <span className="neg">
                      未通过：{assessment.failures.map((f) => `${f.code}(${f.actual ?? "?"} vs ${f.required ?? "?"})`).join("、")}
                    </span>
                  ) : (
                    <span className="pos">全部门槛通过</span>
                  )}
                </div>
              ) : (
                <p className="muted" style={{ fontSize: 13, margin: 0 }}>暂无晋级评估</p>
              )}
            </div>
          );
        })}
      </div>

      {/* Data source status */}
      <h2 className="subheading">数据源状态</h2>
      <div className="card" style={{ marginTop: 8 }}>
        {strategyData.map(({ meta, data }) => {
          const research = data?.researchStatus;
          return (
            <div key={meta.id} style={{ padding: "12px 16px", borderBottom: "1px solid var(--border, #222)" }}>
              <strong>{meta.name}</strong>
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap", fontSize: 13, color: "var(--muted)", marginTop: 4 }}>
                <span className={research?.tushare_production?.passed ? "pos" : "neg"}>
                  Tushare 生产数据：{research?.tushare_production?.passed
                    ? `通过 · ${research.tushare_production.data_cutoff ?? "截止日未知"} · ${research.tushare_production.trading_days ?? 0} 日`
                    : `未通过${research?.tushare_production?.error ? ` · ${research.tushare_production.error}` : ""}`}
                </span>
                <span>
                  公开基准 Qlib：{research?.qlib_benchmark?.passed
                    ? `${research.qlib_benchmark.data_cutoff} 可用`
                    : "未就绪"}
                </span>
                {research?.qlib_benchmark?.results?.linear?.median_rank_ic != null && (
                  <span>
                    Alpha158 线性 RankIC：{research.qlib_benchmark.results.linear.median_rank_ic.toFixed(4)}
                  </span>
                )}
                {research?.model_health && (
                  <span>
                    冠军健康：连续跑输 {research.model_health.consecutive_underperform_days ?? 0}/10 日
                    · 回撤 {research.model_health.current_drawdown_pct != null ? pct(research.model_health.current_drawdown_pct) : "暂无"}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* Agent dispatch runs */}
      <h2 className="subheading">Agent 运行记录</h2>
      <div className="card" style={{ marginTop: 8 }}>
        {agentRuns.length === 0 ? (
          <p className="muted" style={{ padding: 16 }}>暂无 Agent 运行记录</p>
        ) : (
          <div className="table-wrap compact-table">
            <table>
              <thead>
                <tr>
                  <th>任务 ID</th>
                  <th>Agent</th>
                  <th>模式</th>
                  <th>状态</th>
                  <th className="num">耗时</th>
                  <th>变更文件</th>
                </tr>
              </thead>
              <tbody>
                {agentRuns.map((run, i) => (
                  <tr key={i}>
                    <td className="mono">{run.id}</td>
                    <td>{run.agent}</td>
                    <td>{run.mode}</td>
                    <td>
                      <span className={`pill ${run.status === "passed" ? "good" : "bad"}`}>
                        {run.status}
                      </span>
                    </td>
                    <td className="num">{run.duration_seconds?.toFixed(1) ?? "-"}s</td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {run.changed_paths?.length ?? 0} 个文件
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Runtime directory info */}
      <h2 className="subheading">运行目录</h2>
      <div className="card" style={{ marginTop: 8, padding: 16 }}>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 12 }}>
          <div>
            <span className="muted" style={{ fontSize: 12 }}>RUNTIME_DATA_DIR</span>
            <div className="mono" style={{ fontSize: 13 }}>
              {process.env.RUNTIME_DATA_DIR ?? "<cwd>/data/runtime"}
            </div>
          </div>
          <div>
            <span className="muted" style={{ fontSize: 12 }}>PYSERVER_URL</span>
            <div className="mono" style={{ fontSize: 13 }}>
              {process.env.PYSERVER_URL ?? "http://localhost:8001"}
            </div>
          </div>
          <div>
            <span className="muted" style={{ fontSize: 12 }}>部署地址</span>
            <div className="mono" style={{ fontSize: 13 }}>
              {process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3100"}
            </div>
          </div>
          <div>
            <span className="muted" style={{ fontSize: 12 }}>Base Path</span>
            <div className="mono" style={{ fontSize: 13 }}>
              {process.env.NEXT_BASE_PATH ?? "/"}
            </div>
          </div>
        </div>
        {manifest?.file_hashes && Object.keys(manifest.file_hashes).length > 0 && (
          <>
            <h3 style={{ marginTop: 16, fontSize: 14 }}>文件 SHA-256</h3>
            <div className="table-wrap compact-table">
              <table>
                <thead>
                  <tr>
                    <th>文件</th>
                    <th>SHA-256</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(manifest.file_hashes).map(([file, hash]) => (
                    <tr key={file}>
                      <td className="mono">{file}</td>
                      <td className="mono muted" style={{ fontSize: 11 }}>{hash}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
