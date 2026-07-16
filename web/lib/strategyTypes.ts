export interface SymbolSnapshot {
  symbol: string;
  name?: string | null;
  theme?: string;
  note?: string | null;
  closes: number[];
  volumes?: number[];
  global_supply?: boolean | null;
  fundamental?: {
    pe_ttm?: number | null;
    pb?: number | null;
    market_cap?: number | null;
    profit_yoy?: number | null;
  };
}

export interface Signal {
  symbol: string;
  action: "buy" | "hold" | "sell";
  confidence: number;
  size: number;
  rationale: string;
  /** Optional explainability fields supplied by a promoted ML model. */
  modelVersion?: string;
  expectedReturns?: Partial<Record<"d1" | "d3" | "d5" | "d10", number>>;
  downsideRisk?: number;
  reasonCodes?: string[];
}
