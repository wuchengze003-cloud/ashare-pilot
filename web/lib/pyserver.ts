// Typed client for the Python Tushare sidecar. Adds a thin in-process dedupe
// on top of pyserver's own SQLite cache to coalesce burst calls within a render.
const BASE = process.env.PYSERVER_URL ?? "http://localhost:8001";
// Default 180s — Tushare HK endpoints are rate-limited at 2/min, so a few
// HK symbols may need to wait in pyserver's token bucket before being served.
const TIMEOUT_MS = Number(process.env.PYSERVER_TIMEOUT_MS ?? 180_000);

export interface Kline {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export type MinuteKlineFrequency = "1min" | "5min" | "15min" | "30min" | "60min";

export interface MinuteKline {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  amount: number;
}

export interface MinuteKlineSeries {
  symbol: string;
  ts_code: string;
  freq: MinuteKlineFrequency;
  source: "tushare_stk_mins";
  realtime: false;
  bars: MinuteKline[];
}

export interface Fundamental {
  symbol: string;
  name?: string | null;
  pe_ttm?: number | null;
  pb?: number | null;
  market_cap?: number | null;
  revenue_yoy?: number | null;
  profit_yoy?: number | null;
}

const inflight = new Map<string, Promise<unknown>>();

async function get<T>(path: string, params: Record<string, string>): Promise<T> {
  const qs = new URLSearchParams(params).toString();
  const key = `${path}?${qs}`;
  const existing = inflight.get(key);
  if (existing) return existing as Promise<T>;
  const p = (async () => {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
      const r = await fetch(`${BASE}${path}?${qs}`, { cache: "no-store", signal: ctrl.signal });
      if (!r.ok) {
        const body = await r.text();
        console.error("[pyserver] HTTP request failed", { path, status: r.status, params, body: body.slice(0, 200) });
        throw new Error(`pyserver ${path} ${r.status}: ${body}`);
      }
      return (await r.json()) as T;
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") {
        console.error("[pyserver] request timed out", { path, params, timeoutMs: TIMEOUT_MS });
      } else if (!(err instanceof Error && err.message.startsWith("pyserver "))) {
        console.error("[pyserver] request error", { path, params, error: err instanceof Error ? err.message : String(err) });
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  })();
  inflight.set(key, p);
  try {
    return await p;
  } finally {
    // brief dedupe only — release after settle so cache layer below handles repeats
    setTimeout(() => inflight.delete(key), 100);
  }
}

export function fetchKlines(symbol: string, start = "20230101", end?: string) {
  const params: Record<string, string> = { symbol, start, adjust: "qfq" };
  if (end) params.end = end;
  return get<Kline[]>("/klines", params);
}

export function fetchMinuteKlines(
  symbol: string,
  start: string,
  end?: string,
  freq: MinuteKlineFrequency = "1min",
) {
  const params: Record<string, string> = { symbol, start, freq };
  if (end) params.end = end;
  return get<MinuteKlineSeries>("/minute-klines", params);
}

export function fetchFundamental(symbol: string) {
  return get<Fundamental>("/fundamental", { symbol });
}

export interface Analyst {
  symbol: string;
  buy_count?: number | null;
  total_count?: number | null;
  buy_ratio?: number | null;
  consensus_eps_next?: number | null;
  implied_target?: number | null;
  target_price_source?: string | null;
  target_price_method?: string | null;
  target_price_confidence?: number | null;
  target_horizon_days?: number | null;
  current_price?: number | null;
  current_price_source?: string | null;
  current_price_as_of?: string | null;
  upside_pct?: number | null;
}

export function fetchAnalyst(symbol: string) {
  return get<Analyst>("/analyst", { symbol });
}

export function fetchAnalysts(symbols: string[]) {
  const uniq = [...new Set(symbols.map((s) => s.trim()).filter(Boolean))];
  if (uniq.length === 0) return Promise.resolve([] as Analyst[]);
  return get<Analyst[]>("/analysts", { symbols: uniq.join(",") });
}

export function fetchSpot(symbol: string) {
  return get<Spot>(
    "/spot",
    { symbol },
  );
}

export interface Spot {
  symbol: string;
  name: string;
  price: number;
  change_pct: number;
  volume?: number;
  turnover?: number;
  source?: string;
  as_of?: string;
}

export function fetchSpots(symbols: string[]) {
  const uniq = [...new Set(symbols.map((s) => s.trim()).filter(Boolean))];
  if (uniq.length === 0) return Promise.resolve([] as Spot[]);
  return get<Spot[]>("/spots", { symbols: uniq.join(",") });
}

// ---------- Moneyflow (Tide strategy) ----------------------------------------

export interface MoneyflowRow {
  trade_date: string;
  buy_lg_amount: number | null;
  sell_lg_amount: number | null;
  buy_elg_amount: number | null;
  sell_elg_amount: number | null;
  net_mf_amount: number | null;
}

export interface MoneyflowResponse {
  symbol: string;
  rows: MoneyflowRow[];
}

export function fetchMoneyflow(symbol: string, days = 20) {
  return get<MoneyflowResponse>("/moneyflow", { symbol, days: String(days) });
}

// ---------- Index Daily (Prism strategy) -------------------------------------

export interface IndexDailyRow {
  trade_date: string;
  open: number | null;
  close: number | null;
  high: number | null;
  low: number | null;
  pct_chg: number | null;
  vol: number | null;
}

export interface IndexDailyResponse {
  index_code: string;
  index_name: string;
  rows: IndexDailyRow[];
}

export function fetchIndexDaily(index: string, days = 60) {
  return get<IndexDailyResponse>("/index-daily", { index, days: String(days) });
}

// ---------- Market Breadth (Prism strategy) ----------------------------------

export interface MarketBreadth {
  trade_date: string;
  advance_count: number;
  decline_count: number;
  flat_count: number;
  advance_ratio: number;
  new_high_20: number;
  new_low_20: number;
  total_amount: number | null;
  limit_up_count: number;
  limit_down_count: number;
}

export function fetchMarketBreadth() {
  return get<MarketBreadth>("/market-breadth", {});
}

// ---------- Margin Detail (Tide strategy — 融资余额变化) ----------------------

export interface MarginRow {
  trade_date: string;
  rzye: number | null;   // 融资余额(元)
  rqye: number | null;   // 融券余额(元)
  rzmre: number | null;  // 融资买入额(元)
  rqchl: number | null;  // 融券偿还量(股)
}

export interface MarginResponse {
  symbol: string;
  rows: MarginRow[];
}

export function fetchMarginDetail(symbol: string, days = 20) {
  return get<MarginResponse>("/margin-detail", { symbol, days: String(days) });
}

// ---------- Index Weight (Prism strategy — 行业扩散度) ------------------------

export interface IndexWeightRow {
  ts_code: string;
  trade_date: string;
  weight: number | null;
}

export interface IndexWeightResponse {
  index_code: string;
  trade_date: string;
  rows: IndexWeightRow[];
}

export function fetchIndexWeight(index = "hs300") {
  return get<IndexWeightResponse>("/index-weight", { index });
}
