export interface BuySignalHistoryInput {
  signal_date: string;
  signals: Array<{
    symbol: string;
    name?: string | null;
    theme?: string;
    action: "buy" | "hold" | "sell";
    size: number;
    rationale: string;
    signalPrice?: number | null;
    signalPriceDate?: string | null;
  }>;
}

export interface BuySignalHistoryRow {
  signalDate: string;
  symbol: string;
  name: string | null;
  theme: string | null;
  signalPrice: number | null;
  signalPriceDate: string | null;
  currentPrice: number | null;
  currentAsOf: string | null;
  changePct: number | null;
  rationale: string;
}

export interface BuySignalHistorySummary {
  totalSignals: number;
  validSignals: number;
  positiveSignals: number;
  winRatePct: number | null;
  averageChangePct: number | null;
}

export function buildBuySignalHistoryRows(
  history: BuySignalHistoryInput[],
  currentPrices: ReadonlyMap<string, number | null>,
  currentDates: ReadonlyMap<string, string | null>,
): BuySignalHistoryRow[] {
  return history.flatMap((snapshot) =>
    snapshot.signals
      .filter((signal) => signal.action === "buy" && signal.size > 0)
      .map((signal) => {
        const signalPrice = signal.signalPrice ?? null;
        const currentPrice = currentPrices.get(signal.symbol) ?? null;
        const rawChangePct =
          currentPrice != null && signalPrice != null && signalPrice > 0
            ? ((currentPrice - signalPrice) / signalPrice) * 100
            : null;
        return {
          signalDate: snapshot.signal_date,
          symbol: signal.symbol,
          name: signal.name ?? null,
          theme: signal.theme ?? null,
          signalPrice,
          signalPriceDate: signal.signalPriceDate ?? null,
          currentPrice,
          currentAsOf: currentDates.get(signal.symbol) ?? null,
          changePct: rawChangePct != null && Math.abs(rawChangePct) < 0.05 ? 0 : rawChangePct,
          rationale: signal.rationale,
        };
      }),
  );
}

export function summarizeBuySignalHistory(rows: BuySignalHistoryRow[]): BuySignalHistorySummary {
  const changes = rows.flatMap((row) => row.changePct == null ? [] : [row.changePct]);
  const positiveSignals = changes.filter((change) => change > 0).length;
  return {
    totalSignals: rows.length,
    validSignals: changes.length,
    positiveSignals,
    winRatePct: changes.length > 0 ? (positiveSignals / changes.length) * 100 : null,
    averageChangePct:
      changes.length > 0 ? changes.reduce((sum, change) => sum + change, 0) / changes.length : null,
  };
}

export function paginateBuySignalHistory(
  rows: BuySignalHistoryRow[],
  requestedPage: number,
  pageSize: number,
) {
  const safePageSize = Math.max(1, Math.floor(pageSize));
  const totalPages = Math.max(1, Math.ceil(rows.length / safePageSize));
  const page = Math.min(Math.max(1, Math.floor(requestedPage)), totalPages);
  const startIndex = (page - 1) * safePageSize;
  return {
    page,
    pageSize: safePageSize,
    totalPages,
    startIndex,
    endIndex: Math.min(startIndex + safePageSize, rows.length),
    rows: rows.slice(startIndex, startIndex + safePageSize),
  };
}
