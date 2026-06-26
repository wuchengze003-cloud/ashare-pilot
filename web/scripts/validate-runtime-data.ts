import { assertRuntimeArtifacts, readRuntimeValidationInput } from "../lib/runtimeValidation";

const input = readRuntimeValidationInput();
assertRuntimeArtifacts(input);

const latestDate = input.backtest?.latestDate ?? input.backtest?.equityCurve?.at(-1)?.date ?? "unknown";
const historyCount = input.histories.length;
const holdings = Object.keys(input.backtest?.latestHoldings ?? {}).join(",") || "none";

console.log(
  `Runtime validation passed: latest=${latestDate}, histories=${historyCount}, holdings=${holdings}`,
);
