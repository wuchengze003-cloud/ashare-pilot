# ashare-pilot Repository Rules

This repository is the production A-share research and signal product.
The accepted architecture is documented in
[`docs/architecture/`](docs/architecture/README.md). Historical implementations
belong in the separate `ashare-pilot-legacy` repository and must not remain as
production fallbacks, examples, fixtures, or hidden dependencies.

## Non-negotiable Rules

1. Python `research/` is the sole authority for production strategy semantics,
   backtests, portfolio targets, and production signal generation.
2. `web/` presents versioned production artifacts. It must not contain an
   independent strategy or backtest engine.
3. Historical research and production eligibility use point-in-time CSI 800
   membership. The old `web/data/universe.json` belongs to Legacy; any retained
   AI watch data must use a separate versioned annotation contract and never
   grant production trading eligibility.
4. Every market decision must bind an explicit `as_of` or trading date. Never
   substitute the latest snapshot or wall-clock date for point-in-time data.
5. There is no fallback or import from the Legacy repository. Reusable code is
   migrated by reviewed copy with current contracts and tests.
6. Degraded operation follows
   [`ADR-003`](docs/architecture/decisions/ADR-003-degraded-operation.md).
   Stale data blocks new decisions; it does not by itself authorize a blind
   liquidation.
7. Shared transaction costs, trading constraints, signal schemas, and runtime
   contracts are controlled interfaces. Changes require a focused review and
   contract tests in every consuming language.
8. Agents work only in isolated worktrees and within machine-enforced paths.
   A prompt, `context_files`, or prose prohibition is not a security boundary.
9. Weak models may implement already-defined behavior. They may not define or
   change strategy semantics, contracts, promotion gates, execution rules,
   universes, production runtime, secrets, or deployment behavior.
10. Never reset, clean, stash, overwrite, or merge unrelated user or agent work.
11. Production and Legacy repositories must not share writable databases,
    runtime directories, generated artifacts, source imports, symlinks, or
    deployment state.

## Repository Map

| Path | Responsibility | Local rules |
|---|---|---|
| `research/` | Authoritative research, backtest, promotion, portfolio and production signals | [`research/AGENTS.md`](research/AGENTS.md) |
| `pyserver/` | Market-data acquisition, normalization, caching and data-quality endpoints | [`pyserver/AGENTS.md`](pyserver/AGENTS.md) |
| `web/` | Product UI and read-only production APIs | [`web/AGENTS.md`](web/AGENTS.md) |
| `ops/` | Orchestration, agent dispatch, deployment and operational evidence | [`ops/AGENTS.md`](ops/AGENTS.md) |
| `config/` | Shared cost and trading-constraint definitions | Root rules apply |
| `docs/architecture/` | Accepted decisions, contracts and ownership | Architecture owner review |

Files classified for extraction in
[`PROJECT_SPLIT.md`](docs/architecture/PROJECT_SPLIT.md) are not valid
production dependencies even while the physical split is in progress.

## Change Classification

- **Leaf change:** one subsystem, no contract or production-semantic change.
  It may be delegated with narrow paths and an objective acceptance command.
- **Contract change:** affects data shape, meaning, freshness, versioning, cost,
  execution, universe membership, or cross-system behavior. Use a focused
  change reviewed by the architecture owner.
- **Production change:** affects strategy, portfolio, promotion, signal
  activation, deployment, or degraded behavior. It requires strong-model
  review, full relevant tests, and explicit human approval.

## Required Handoff

Every implementation handoff must state the base commit, changed paths,
commands actually run, results, remaining risks, and whether any runtime,
universe, strategy, contract, or deployment state changed.
