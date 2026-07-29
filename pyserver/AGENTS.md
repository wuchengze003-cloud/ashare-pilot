# Pyserver Rules

`pyserver/` owns market-data access, normalization, caching, freshness, and
data-quality interfaces. It does not own strategy or portfolio decisions.

## Rules

1. Return explicit source date, trading date, adjustment mode, units, and
   freshness where relevant. Never silently substitute an older snapshot.
2. Upstream failure, missing fields, partial pages, and stale caches must be
   observable. Do not convert them into valid-looking empty data.
3. Point-in-time endpoints must accept or derive an explicit `as_of` under a
   documented trading-calendar rule.
4. Do not rank stocks, select strategies, size positions, or decide degraded
   trading state.
5. HTTP or versioned data artifacts are the target cross-system interface.
   Consumers must not depend on SQLite table layout.
6. Credentials stay in local environment files and never appear in logs,
   fixtures, manifests, or agent prompts.
7. Endpoint shape or meaning changes are contract changes and require consumer
   tests. Weak models may implement an already-approved endpoint contract but
   may not define its semantics.
8. Do not access the Legacy repository or share its SQLite files, caches,
   runtime directories, credentials, or provider exports.

## Validation

```bash
uv run python -m unittest test_main
uv run python -m py_compile main.py
```

Tests that call an external provider must be clearly separated from deterministic
unit and contract tests.
