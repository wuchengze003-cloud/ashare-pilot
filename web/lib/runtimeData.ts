import fs from "node:fs";
import path from "node:path";

export function runtimeDataDir(): string {
  const configured = process.env.RUNTIME_DATA_DIR;
  if (configured && configured.trim()) {
    return path.isAbsolute(configured)
      ? configured
      : path.resolve(process.cwd(), configured);
  }
  return path.resolve(process.cwd(), "data", "runtime");
}

export function runtimeDataPath(name: string): string {
  return path.join(runtimeDataDir(), name);
}

/**
 * Per-strategy runtime directory: `<runtimeDir>/strategies/<strategyId>/`.
 * Each strategy gets its own backtest.json, signals.json, and history/.
 */
export function runtimeStrategyDir(strategyId: string): string {
  return path.join(runtimeDataDir(), "strategies", strategyId);
}

export function runtimeStrategyPath(strategyId: string, name: string): string {
  return path.join(runtimeStrategyDir(strategyId), name);
}

/** Read a JSON file from a strategy-specific runtime directory. */
export function readStrategyJson<T>(strategyId: string, name: string): T | null {
  const file = runtimeStrategyPath(strategyId, name);
  if (!fs.existsSync(file)) return null;
  return JSON.parse(fs.readFileSync(file, "utf-8")) as T;
}

/** Write a JSON file to a strategy-specific runtime directory. */
export function writeStrategyJson(strategyId: string, name: string, value: unknown): void {
  const file = runtimeStrategyPath(strategyId, name);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + "\n", "utf-8");
}

export function readRuntimeJson<T>(name: string): T | null {
  const file = runtimeDataPath(name);
  if (!fs.existsSync(file)) return null;
  return JSON.parse(fs.readFileSync(file, "utf-8")) as T;
}

export function writeRuntimeJson(name: string, value: unknown): void {
  const file = runtimeDataPath(name);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, JSON.stringify(value, null, 2) + "\n", "utf-8");
}

export function listRuntimeJson<T>(dirName: string): Array<{ name: string; data: T }> {
  const dir = runtimeDataPath(dirName);
  if (!fs.existsSync(dir)) return [];
  return fs.readdirSync(dir)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .map((name) => ({
      name,
      data: JSON.parse(fs.readFileSync(path.join(dir, name), "utf-8")) as T,
    }));
}
