# Research Rules

`research/` is the sole authority for production strategy semantics, point-in-
time backtests, portfolio targets, promotion, and production signal generation.

## Rules

1. Every research row and decision must have explicit point-in-time membership
   and source dates. Never use today's constituents, fundamentals, or labels in
   an earlier decision.
2. General research and production eligibility use point-in-time CSI 800
   membership. The AI watch list is not a production eligibility source.
3. Parameter selection may use only preregistered development and validation
   periods. Final/OOS/frozen samples cannot be reused to revise formulas,
   parameters, gates, or universes.
4. Record every candidate family, parameter trial, data hash, code hash, cost
   version, universe version, and acceptance result needed to assess selection
   bias.
5. Production and backtest use the shared cost and trading-constraint
   definitions. Missing limits, suspension status, adjustment factors, dates,
   or quality evidence fail closed.
6. No candidate becomes active without a production inference adapter that
   reproduces its exact feature, ranking, portfolio, and execution semantics.
7. Strategy, promotion, universe, execution, final-sample, and registry files
   are not weak-model tasks.
8. Runtime and registry artifacts are never edited to manufacture a pass.
9. Do not import, execute, compare against, or fallback to code in
   `ashare-pilot-legacy`. A reusable component must be migrated with current
   contracts and tests before Research may depend on it.
10. Historical experiment outputs and dated reports are external audit
    artifacts, not tracked source files.

## Validation

For Research changes run the focused tests first, then when relevant:

```bash
uv run pytest -q
uv run ruff check ashare_research tests
```

Any result reported as production evidence must also identify its data cutoff,
universe, cost contract, code/config hashes, and whether the sample was used
for selection or final acceptance.
