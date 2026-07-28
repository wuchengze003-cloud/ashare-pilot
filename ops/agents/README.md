# Agent jobs

Agent jobs use reviewed JSON manifests. Read-only jobs must leave the current
worktree unchanged. Coding jobs receive a new Git worktree and are rejected if
they modify files outside `allowed_paths` or touch a forbidden path.

```bash
python3 ops/agents/dispatch.py ops/agents/manifests/daily-data-quality.json
```

Runtime reports and generated worktrees are ignored by Git. Agents never
promote a model, edit the production universe, or deploy the application.

Every manifest declares an input-data cutoff, expected outputs, allowed and
forbidden paths, tests, and a maximum subagent count. Read-only jobs fingerprint
both Git-visible files and protected ignored runtime paths. Coding jobs remain
in their worktree until Hermes reviews the diff and test evidence.
They refuse to start when their allowed paths contain uncommitted changes,
because an isolated worktree starts from `HEAD` and must not silently miss
uncommitted V2 code. Unrelated personal files do not block a research job.
