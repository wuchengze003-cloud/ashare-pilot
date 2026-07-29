# Web Rules

`web/` is the product presentation layer and read-only API facade. It does not
contain a strategy, optimizer, backtest, portfolio, or execution engine.

## Rules

1. Read production state only from versioned, date-bound artifacts or explicit
   APIs. Validate schema, champion, contract hash, and expected trading date.
2. Never recompute production ranking, target weights, strategy performance,
   or degraded state in UI components.
3. Diagnostics displayed by Web are immutable Python-produced artifacts. Do
   not add or retain TypeScript strategy or backtest semantics.
4. Do not read `pyserver/cache.db`, Research internal files, unversioned runtime
   paths, or any path in the Legacy project.
5. Web does not own production eligibility or a tradable universe file.
6. Empty signals are not a state. Render `ACTIVE`, `HOLD`, `REDUCE_ONLY`, and
   `FLAT` from the explicit production contract.
7. Keep route handlers thin and domain-independent. Shared presentation logic
   belongs in `web/lib/`; financial calculations belong to Research.
8. Changes touching production signal interpretation require strong-model
   review and contract tests. Weak models may work on isolated components,
   copy, styles, types, and behavior already fixed by tests.

## Validation

For Web changes run:

```bash
npm test
./node_modules/.bin/tsc --noEmit
npm run build
```

Run only the commands relevant to a narrow leaf change during iteration, but
the handoff must state exactly what was and was not run.
