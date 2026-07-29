import type { ProductionCandidateSummary } from "./productionGate";

export const productionCandidateNames: Record<string, string> = {
  "harbor-v1": "稳健价值",
  "surge-v1": "趋势参与",
  "flow-v1": "资金流",
};

export const productionFamilyNames: Record<string, string> = {
  defensive_value_flow: "低波动价值与资金确认",
  confirmed_risk_on_participation: "市场确认后的趋势参与",
  persistent_capital_flow: "持续资金流积累",
};

export const productionReasonLabels: Record<string, string> = {
  DAILY_REPORT_MISSING: "日线赛马报告缺失",
  DAILY_REPORT_INVALID: "日线赛马报告不完整",
  MINUTE_REPORT_MISSING: "分钟执行报告缺失",
  MINUTE_REPORT_INVALID: "分钟执行报告不完整",
  RACE_CONTRACT_MISMATCH: "研究合同版本不一致",
  RACE_CONTRACT_MISSING: "研究合同指纹缺失",
  RACE_CONTRACT_STALE: "研究报告与当前代码合同不一致",
  MINUTE_DATA_INCOMPLETE: "固定数据包未覆盖全部预注册分钟样本",
  NO_PRODUCTION_CHAMPION: "三条候选尚无一条通过全部生产门禁",
  CHAMPION_ID_MISSING: "冠军身份缺失",
  CHAMPION_GATE_MISMATCH: "冠军证据与门禁结果不一致",
  CHAMPION_DAILY_GATE_MISMATCH: "冠军与日线准入结果冲突",
  CHAMPION_FREQUENCY_MISMATCH: "冠军执行频率与研究合同不一致",
  CHAMPION_IMPLEMENTATION_MISSING: "冠军推理实现尚未接入生产链路",
  PRODUCTION_GATE_MISSING: "生产门禁快照尚未生成",
  PRODUCTION_GATE_INVALID: "生产门禁快照无效",
};

export const candidateGateLabels: Record<string, string> = {
  data_quality: "数据质量",
  validation_trading_days: "验证期样本长度",
  oos_trading_days: "样本外长度",
  frozen_trading_days: "冻结期长度",
  validation_sharpe: "验证期 Sharpe",
  validation_drawdown: "验证期最大回撤",
  oos_folds: "样本外滚动折数",
  closed_trades: "样本外交易数",
  oos_sharpe: "样本外 Sharpe",
  frozen_sharpe: "冻结期 Sharpe",
  median_oos_fold_sharpe: "样本外折中位 Sharpe",
  positive_oos_fold_share: "样本外正收益折占比",
  oos_annualized_return: "样本外年化收益",
  oos_calmar: "样本外 Calmar",
  oos_drawdown: "样本外最大回撤",
  frozen_drawdown: "冻结期最大回撤",
  upside_capture: "上涨捕获",
  downside_capture: "下跌捕获",
  bootstrap: "Bootstrap 置信度",
  double_cost_positive: "双倍成本后收益",
  double_cost_oos_sharpe: "双倍成本后 Sharpe",
};

export function productionCandidateName(candidateId: string): string {
  return productionCandidateNames[candidateId] ?? candidateId;
}

export function productionFamilyName(family: string): string {
  return productionFamilyNames[family] ?? family;
}

export function productionReasonLabel(code: string): string {
  return productionReasonLabels[code] ?? code;
}

export function candidateGateLabel(code: string): string {
  return candidateGateLabels[code] ?? code;
}

export function productionCandidateStatus(
  candidate: ProductionCandidateSummary,
  championId: string | null,
): string {
  if (candidate.candidate_id === championId) return "唯一上线";
  if (candidate.daily_gates_passed) return "通过候选门禁";
  return "未通过";
}
