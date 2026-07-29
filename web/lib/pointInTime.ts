export interface TradeDatedRow {
  trade_date: string;
}

export function compactTradeDate(value: string): string {
  return value.replaceAll("-", "");
}

export function rowsAtOrBefore<T extends TradeDatedRow>(
  rows: T[] | undefined,
  asOf: string,
  limit?: number,
): T[] {
  if (!rows || rows.length === 0) return [];
  const cutoff = compactTradeDate(asOf);
  const visible = rows
    .filter((row) => compactTradeDate(row.trade_date) <= cutoff)
    .sort((a, b) => compactTradeDate(a.trade_date).localeCompare(compactTradeDate(b.trade_date)));
  return limit === undefined ? visible : visible.slice(-limit);
}

export function latestRowAtOrBefore<T extends TradeDatedRow>(
  rows: T[] | undefined,
  asOf: string,
): T | undefined {
  return rowsAtOrBefore(rows, asOf).at(-1);
}
