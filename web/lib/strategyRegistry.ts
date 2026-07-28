// Strategy registry — central catalogue of all available trading strategies.
// Each strategy exposes a Scorer factory and metadata for the frontend.
import type { Scorer } from "./backtest";
import { ruleBasedScorer } from "./dashboardBacktest";
import { tideScorer } from "./strategies/tide";
import { prismScorer } from "./strategies/prism";

export interface StrategyMeta {
  /** Unique machine-readable key. */
  id: string;
  /** Human-readable Chinese name. */
  name: string;
  /** English codename (model-style naming). */
  codename: string;
  /** One-line description. */
  description: string;
  /** Factor composition summary for display. */
  factors: string[];
  /** Default minimum score threshold for buy signals. */
  defaultMinScore: number;
  /** Factory that returns a Scorer instance. */
  createScorer: (opts?: Record<string, unknown>) => Scorer;
}

export const STRATEGIES: StrategyMeta[] = [
  {
    id: "momentum-v1",
    name: "右侧动量",
    codename: "Momentum-V1",
    description: "原始右侧趋势策略：价格动量 35% + 主题强度 30% + 量能确认 20% + 趋势形态 15%",
    factors: ["价格动量", "主题强度", "量能确认", "趋势形态"],
    defaultMinScore: 0.54,
    createScorer: (opts) => ruleBasedScorer(opts as never),
  },
  {
    id: "tide",
    name: "潮汐",
    codename: "Tide",
    description: "资金流微观结构策略：通过量价关系推导机构资金进出，捕捉主力建仓/出货节奏",
    factors: ["资金流动量", "量价背离", "OBV趋势", "大单代理", "吸筹/派发"],
    defaultMinScore: 0.56,
    createScorer: (opts) => tideScorer(opts as never),
  },
  {
    id: "prism",
    name: "棱镜",
    codename: "Prism",
    description: "自适应多因子策略：基于市场状态检测动态旋转因子权重，趋势市追动量、震荡市做均值回归",
    factors: ["状态检测", "因子旋转", "波动率目标", "市场宽度", "均值回归"],
    defaultMinScore: 0.55,
    createScorer: (opts) => prismScorer(opts as never),
  },
];

export function getStrategy(id: string): StrategyMeta | undefined {
  return STRATEGIES.find((s) => s.id === id);
}

export function getDefaultStrategy(): StrategyMeta {
  return STRATEGIES[0];
}
