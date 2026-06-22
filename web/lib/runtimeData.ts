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
