import fs from "node:fs";
import path from "node:path";

export function repoConfigPath(name: string): string {
  const candidates = [
    path.resolve(process.cwd(), "..", "config", name),
    path.resolve(process.cwd(), "config", name),
    path.resolve(__dirname, "..", "..", "config", name),
  ];
  const resolved = candidates.find((candidate) => fs.existsSync(candidate));
  if (!resolved) {
    throw new Error(
      `shared config ${name} is missing; checked ${candidates.join(", ")}`,
    );
  }
  return resolved;
}
