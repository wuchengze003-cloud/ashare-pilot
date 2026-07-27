import type { Kline, Spot } from "./pyserver";

function dateFromSpot(spot: Spot | null | undefined): string | null {
  const raw = spot?.as_of;
  if (!raw) return null;
  const m = String(raw).match(/^\d{4}-\d{2}-\d{2}/);
  return m ? m[0] : null;
}

function shanghaiNowParts() {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(new Date());
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "00";
  return {
    date: `${get("year")}-${get("month")}-${get("day")}`,
    hour: Number(get("hour")),
    minute: Number(get("minute")),
  };
}

function isWeekday(date: string): boolean {
  const day = new Date(`${date}T12:00:00+08:00`).getUTCDay();
  return day >= 1 && day <= 5;
}

function canUseSpotDate(date: string, lastKlineDate: string | null): boolean {
  if (lastKlineDate && date <= lastKlineDate) return true;
  if (!isWeekday(date)) return false;

  const now = shanghaiNowParts();
  if (date > now.date) return false;
  if (date < now.date) return true;

  // Do not create a synthetic current-day bar before A-share continuous trading starts.
  return now.hour > 9 || (now.hour === 9 && now.minute >= 30);
}

export function mergeSpotIntoKlines(
  klines: Kline[],
  spot: Spot | null | undefined,
  fallbackDate: string,
): Kline[] {
  const price = spot?.price;
  if (price == null || !Number.isFinite(price) || price <= 0) return klines;
  const date = dateFromSpot(spot) ?? fallbackDate;
  const lastKlineDate = klines.at(-1)?.date ?? null;
  if (!canUseSpotDate(date, lastKlineDate)) return klines;
  if (klines.length === 0) {
    return [{
      date,
      open: price,
      high: price,
      low: price,
      close: price,
      volume: spot?.volume ?? 0,
    }];
  }

  const out = klines.slice();
  const last = out[out.length - 1];
  if (last.date === date) {
    out[out.length - 1] = {
      ...last,
      high: Math.max(last.high, price),
      low: Math.min(last.low, price),
      close: price,
      volume: spot?.volume ?? last.volume,
    };
  } else if (last.date < date) {
    out.push({
      date,
      open: price,
      high: price,
      low: price,
      close: price,
      volume: spot?.volume ?? 0,
    });
  }
  return out;
}
