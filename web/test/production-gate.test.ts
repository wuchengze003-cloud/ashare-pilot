import assert from "node:assert/strict";
import test from "node:test";
import {
  buildCashOnlyProductionSignals,
  buildProductionSignalsApiPayload,
  deriveProductionGate,
  type DailyRaceReport,
  type MinuteRaceReport,
  type ProductionGateSnapshot,
  type ProductionSignalsSnapshot,
} from "../lib/productionGate";

const generatedAt = "2026-07-28T10:00:00.000Z";
const daily: DailyRaceReport = {
  schema_version: 2,
  status: "daily_phase_complete_pending_three_candidate_minute_race",
  contract_sha256: "contract",
  candidates: [
    {
      candidate_id: "anchor-v1",
      family: "defensive_value_anchor",
      daily_gates_passed: false,
      oos: { sharpe: 0.49, max_drawdown_pct: -8.5, closed_trades: 70 },
      frozen: { sharpe: 0.91, max_drawdown_pct: -6.6 },
      oos_upside_capture: 0.046,
    },
    {
      candidate_id: "tide-v3",
      family: "capital_flow_accumulation",
      daily_gates_passed: false,
      oos: { sharpe: 0.57, max_drawdown_pct: -6, closed_trades: 155 },
      frozen: { sharpe: 0.74, max_drawdown_pct: -8.9 },
      oos_upside_capture: 0.048,
    },
    {
      candidate_id: "prism-v3",
      family: "panic_recovery_reversal",
      daily_gates_passed: false,
      oos: { sharpe: 1.82, max_drawdown_pct: -3.7, closed_trades: 29 },
      frozen: { sharpe: 1.52, max_drawdown_pct: -8.3 },
      oos_upside_capture: 0.208,
    },
  ],
};

const passingDaily: DailyRaceReport = {
  ...daily,
  candidates: (daily.candidates as Array<Record<string, unknown>>).map((candidate) => ({
    ...candidate,
    daily_gates_passed: candidate.candidate_id === "prism-v3",
  })),
};

test("production gate fails closed when minute coverage is incomplete", () => {
  const minute: MinuteRaceReport = {
    schema_version: 2,
    status: "blocked_minute_data_coverage",
    contract_sha256: "contract",
    minute_data_sha256: null,
    required_symbol_days: 15_436,
    available_symbol_days: 570,
    minute_coverage_pct: 3.6927,
    missing_symbol_days: 14_866,
    candidates: [],
    production_champion: null,
  };

  const gate = deriveProductionGate(daily, minute, { generatedAt });

  assert.equal(gate.status, "cash-only");
  assert.equal(gate.champion_id, null);
  assert.equal(gate.candidates.length, 3);
  assert.ok(gate.reason_codes.includes("MINUTE_DATA_INCOMPLETE"));
  assert.ok(gate.reason_codes.includes("NO_PRODUCTION_CHAMPION"));
});

test("production gate requires an integrated champion and deployed implementation", () => {
  const minute: MinuteRaceReport = {
    schema_version: 2,
    status: "production_champion_selected",
    contract_sha256: "contract",
    minute_data_sha256: "minute-sha",
    required_symbol_days: 100,
    available_symbol_days: 100,
    minute_coverage_pct: 100,
    missing_symbol_days: 0,
    candidates: [{ candidate_id: "prism-v3", integrated_gates_passed: true }],
    production_champion: "prism-v3",
  };

  const blocked = deriveProductionGate(passingDaily, minute, { generatedAt });
  assert.equal(blocked.status, "cash-only");
  assert.ok(blocked.reason_codes.includes("CHAMPION_IMPLEMENTATION_MISSING"));

  const active = deriveProductionGate(passingDaily, minute, {
    generatedAt,
    deployableChampionIds: ["prism-v3"],
  });
  assert.equal(active.status, "active");
  assert.equal(active.champion_id, "prism-v3");
  assert.deepEqual(active.reason_codes, []);
});

test("cash-only API payload can never leak legacy buy signals", () => {
  const gate = deriveProductionGate(daily, null, { generatedAt });
  const snapshot = buildCashOnlyProductionSignals(
    gate,
    "2026-07-28",
    "2026-07-28",
    "latest-complete-close",
  );
  const payload = buildProductionSignalsApiPayload(gate, {
    ...snapshot,
    signals: [
      {
        symbol: "000001",
        action: "buy",
        confidence: 0.9,
        size: 0.25,
        rationale: "legacy signal must be blocked",
      },
    ],
  });

  assert.equal(payload.status, "cash-only");
  assert.equal(payload.gross_buy_weight, 0);
  assert.deepEqual(payload.signals, []);
  assert.deepEqual(payload.counts, { buy: 0, hold: 0, sell: 0 });
});

test("active API payload rejects a mismatched champion snapshot", () => {
  const minute: MinuteRaceReport = {
    schema_version: 2,
    status: "production_champion_selected",
    contract_sha256: "contract",
    minute_data_sha256: "minute-sha",
    required_symbol_days: 100,
    available_symbol_days: 100,
    minute_coverage_pct: 100,
    missing_symbol_days: 0,
    candidates: [{ candidate_id: "prism-v3", integrated_gates_passed: true }],
    production_champion: "prism-v3",
  };
  const gate = deriveProductionGate(passingDaily, minute, {
    generatedAt,
    deployableChampionIds: ["prism-v3"],
  });
  const payload = buildProductionSignalsApiPayload(gate, {
    schema_version: 2,
    generated_at: generatedAt,
    gate_generated_at: generatedAt,
    contract_sha256: "contract",
    status: "active",
    champion_id: "tide-v3",
    signal_date: "2026-07-28",
    latest_complete_date: "2026-07-28",
    signal_basis: "latest-complete-close",
    reason_codes: [],
    signals: [
      {
        symbol: "000001",
        action: "buy",
        confidence: 0.9,
        size: 0.2,
        rationale: "wrong champion",
      },
    ],
  });

  assert.equal(payload.status, "cash-only");
  assert.equal(payload.stale, true);
  assert.deepEqual(payload.signals, []);
  assert.ok(payload.reason_codes.includes("PRODUCTION_SIGNALS_MISSING_OR_MISMATCHED"));
});

test("active champion may deterministically publish no trades", () => {
  const minute: MinuteRaceReport = {
    schema_version: 2,
    status: "production_champion_selected",
    contract_sha256: "contract",
    minute_data_sha256: "minute-sha",
    required_symbol_days: 100,
    available_symbol_days: 100,
    minute_coverage_pct: 100,
    missing_symbol_days: 0,
    candidates: [{ candidate_id: "prism-v3", integrated_gates_passed: true }],
    production_champion: "prism-v3",
  };
  const gate = deriveProductionGate(passingDaily, minute, {
    generatedAt,
    deployableChampionIds: ["prism-v3"],
  });
  const payload = buildProductionSignalsApiPayload(gate, {
    schema_version: 2,
    generated_at: generatedAt,
    gate_generated_at: generatedAt,
    contract_sha256: "contract",
    status: "active",
    champion_id: "prism-v3",
    signal_date: "2026-07-28",
    latest_complete_date: "2026-07-28",
    signal_basis: "latest-complete-close",
    reason_codes: [],
    signals: [],
  }, null, { now: new Date("2026-07-28T20:00:00+08:00") });

  assert.equal(payload.status, "active");
  assert.equal(payload.champion_id, "prism-v3");
  assert.equal(payload.gross_buy_weight, 0);
  assert.deepEqual(payload.signals, []);
});

test("minute evidence cannot revive a candidate that failed the daily gate", () => {
  const gate = deriveProductionGate(daily, {
    schema_version: 2,
    status: "production_champion_selected",
    contract_sha256: "contract",
    minute_data_sha256: "minute-sha",
    required_symbol_days: 100,
    available_symbol_days: 100,
    minute_coverage_pct: 100,
    missing_symbol_days: 0,
    candidates: [{ candidate_id: "prism-v3", integrated_gates_passed: true }],
    production_champion: "prism-v3",
  }, {
    generatedAt,
    deployableChampionIds: ["prism-v3"],
  });

  assert.equal(gate.status, "cash-only");
  assert.ok(gate.reason_codes.includes("CHAMPION_DAILY_GATE_MISMATCH"));
});

test("daily-only race ignores stale minute reports and can activate its champion", () => {
  const dailyOnly: DailyRaceReport = {
    schema_version: 3,
    status: "production_champion_selected",
    contract_sha256: "daily-contract",
    minute_candidates: [],
    selected_daily_candidate: "harbor-v1",
    production_champion: "harbor-v1",
    candidates: [
      {
        candidate_id: "harbor-v1",
        family: "defensive_value_flow",
        signal_frequency: "1d",
        daily_gates_passed: true,
        oos: {
          annualized_return_pct: 18,
          sharpe: 1.8,
          max_drawdown_pct: -9,
          calmar: 2,
          closed_trades: 50,
        },
        frozen: {
          total_return_pct: 3,
          sharpe: 1.1,
          max_drawdown_pct: -5,
        },
        oos_upside_capture: 0.8,
        positive_oos_fold_share: 0.8,
        bootstrap_probability_sharpe_positive: 0.98,
        daily_gate_results: {
          oos_sharpe: true,
          upside_capture: true,
          bootstrap: true,
        },
      },
      {
        candidate_id: "surge-v1",
        family: "confirmed_risk_on_participation",
        signal_frequency: "1d",
        daily_gates_passed: false,
        daily_gate_results: {
          oos_sharpe: false,
          bootstrap: false,
        },
      },
      {
        candidate_id: "flow-v1",
        family: "persistent_capital_flow",
        signal_frequency: "1d",
        daily_gates_passed: false,
      },
    ],
  };
  const staleMinute: MinuteRaceReport = {
    schema_version: 2,
    status: "blocked_minute_data_coverage",
    contract_sha256: "old-contract",
    minute_coverage_pct: 3.6,
    missing_symbol_days: 10,
  };

  const gate = deriveProductionGate(dailyOnly, staleMinute, {
    generatedAt,
    deployableChampionIds: ["harbor-v1"],
  });

  assert.equal(gate.status, "active");
  assert.equal(gate.champion_id, "harbor-v1");
  assert.equal(gate.contract_sha256, "daily-contract");
  assert.equal(gate.minute_status, null);
  assert.equal(gate.minute_coverage_pct, null);
  assert.deepEqual(gate.reason_codes, []);
  assert.deepEqual(gate.candidates[0], {
    candidate_id: "harbor-v1",
    family: "defensive_value_flow",
    signal_frequency: "1d",
    daily_gates_passed: true,
    oos_sharpe: 1.8,
    frozen_sharpe: 1.1,
    oos_annualized_return_pct: 18,
    frozen_total_return_pct: 3,
    oos_max_drawdown_pct: -9,
    frozen_max_drawdown_pct: -5,
    oos_calmar: 2,
    oos_upside_capture_pct: 80,
    positive_oos_fold_share_pct: 80,
    bootstrap_probability_pct: 98,
    oos_closed_trades: 50,
    failed_gate_codes: [],
  });
  assert.deepEqual(
    gate.candidates[1].failed_gate_codes,
    ["bootstrap", "oos_sharpe"],
  );
});

test("daily-only race remains cash-only without an exact implementation", () => {
  const dailyOnly: DailyRaceReport = {
    schema_version: 3,
    status: "production_champion_selected",
    contract_sha256: "daily-contract",
    minute_candidates: [],
    selected_daily_candidate: "flow-v1",
    production_champion: "flow-v1",
    candidates: [
      {
        candidate_id: "harbor-v1",
        family: "defensive_value_flow",
        signal_frequency: "1d",
        daily_gates_passed: false,
      },
      {
        candidate_id: "surge-v1",
        family: "confirmed_risk_on_participation",
        signal_frequency: "1d",
        daily_gates_passed: false,
      },
      {
        candidate_id: "flow-v1",
        family: "persistent_capital_flow",
        signal_frequency: "1d",
        daily_gates_passed: true,
      },
    ],
  };

  const gate = deriveProductionGate(dailyOnly, null, { generatedAt });

  assert.equal(gate.status, "cash-only");
  assert.ok(gate.reason_codes.includes("CHAMPION_IMPLEMENTATION_MISSING"));
  assert.ok(!gate.reason_codes.includes("MINUTE_REPORT_MISSING"));
});

function activeGateFixture(): ProductionGateSnapshot {
  const minute: MinuteRaceReport = {
    schema_version: 2,
    status: "production_champion_selected",
    contract_sha256: "contract",
    minute_data_sha256: "minute-sha",
    required_symbol_days: 100,
    available_symbol_days: 100,
    minute_coverage_pct: 100,
    missing_symbol_days: 0,
    candidates: [{ candidate_id: "prism-v3", integrated_gates_passed: true }],
    production_champion: "prism-v3",
  };
  return deriveProductionGate(passingDaily, minute, {
    generatedAt,
    deployableChampionIds: ["prism-v3"],
  });
}

function activeSnapshotFixture(
  gate: ProductionGateSnapshot,
  signalDate: string,
): ProductionSignalsSnapshot {
  return {
    schema_version: 2,
    generated_at: generatedAt,
    gate_generated_at: gate.generated_at,
    contract_sha256: gate.contract_sha256,
    status: "active",
    champion_id: gate.champion_id,
    signal_date: signalDate,
    latest_complete_date: signalDate,
    signal_basis: "latest-complete-close",
    reason_codes: [],
    signals: [
      {
        symbol: "000001",
        action: "buy",
        confidence: 0.9,
        size: 0.2,
        rationale: "freshness fixture",
      },
    ],
  };
}

// Asia/Shanghai 2026-07-28 20:00, i.e. after the 2026-07-28 close.
const freshnessNow = new Date("2026-07-28T20:00:00+08:00");

test("stale production signals are served cash-only with an explicit reason", () => {
  const gate = activeGateFixture();
  const snapshot = activeSnapshotFixture(gate, "2026-07-01");
  const payload = buildProductionSignalsApiPayload(gate, snapshot, null, {
    now: freshnessNow,
  });

  assert.equal(payload.status, "cash-only");
  assert.equal(payload.champion_id, null);
  assert.equal(payload.stale, true);
  assert.ok(payload.reason_codes.includes("PRODUCTION_SIGNALS_STALE"));
  assert.deepEqual(payload.signals, []);
  assert.equal(payload.gross_buy_weight, 0);
});

test("signals inside the freshness window stay active", () => {
  const gate = activeGateFixture();
  const snapshot = activeSnapshotFixture(gate, "2026-07-20");
  const payload = buildProductionSignalsApiPayload(gate, snapshot, null, {
    now: freshnessNow,
  });

  assert.equal(payload.status, "active");
  assert.equal(payload.stale, false);
  assert.equal(payload.signals.length, 1);
  assert.equal(payload.gross_buy_weight, 0.2);
});

test("a snapshot exactly at the age limit is still fresh", () => {
  const gate = activeGateFixture();
  const snapshot = activeSnapshotFixture(gate, "2026-07-13");
  const payload = buildProductionSignalsApiPayload(gate, snapshot, null, {
    now: freshnessNow,
  });

  assert.equal(payload.status, "active");
  assert.equal(payload.stale, false);
});

test("the freshness window is configurable and fails closed beyond it", () => {
  const gate = activeGateFixture();
  const snapshot = activeSnapshotFixture(gate, "2026-07-20");
  const payload = buildProductionSignalsApiPayload(gate, snapshot, null, {
    now: freshnessNow,
    maxAgeDays: 5,
  });

  assert.equal(payload.status, "cash-only");
  assert.equal(payload.stale, true);
  assert.ok(payload.reason_codes.includes("PRODUCTION_SIGNALS_STALE"));
  assert.deepEqual(payload.signals, []);
});

test("a snapshot with an invalid signal date is treated as stale", () => {
  const gate = activeGateFixture();
  const snapshot = activeSnapshotFixture(gate, "not-a-date");
  const payload = buildProductionSignalsApiPayload(gate, snapshot, null, {
    now: freshnessNow,
  });

  assert.equal(payload.status, "cash-only");
  assert.equal(payload.stale, true);
  assert.ok(payload.reason_codes.includes("PRODUCTION_SIGNALS_STALE"));
  assert.deepEqual(payload.signals, []);
});
