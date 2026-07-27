// Run `fn(item)` over `items` with at most `limit` in flight. Preserves order.
export async function mapPool<T, R>(
  items: T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const results: R[] = new Array(items.length);
  let next = 0;
  async function worker() {
    while (true) {
      const i = next++;
      if (i >= items.length) return;
      results[i] = await fn(items[i], i);
    }
  }
  // Clamp to >= 1: a zero/negative/NaN limit would otherwise spawn no workers
  // and silently resolve to an array of `undefined` (Array.from length NaN -> 0).
  const workerCount = Math.max(
    1,
    Math.min(Number.isFinite(limit) ? Math.floor(limit) : 1, items.length),
  );
  const workers = Array.from({ length: workerCount }, worker);
  await Promise.all(workers);
  return results;
}
