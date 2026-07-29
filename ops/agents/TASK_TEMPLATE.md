# Agent Task Template

Use one manifest and one isolated worktree per task. Keep the task small enough
that a reviewer can understand the complete diff and objective evidence.

```yaml
task_id: YYYY-MM-DD-short-name
base_commit: full-40-character-git-sha
model_tier: cheap | standard | strong

goal: one observable outcome
non_goals:
  - behavior that must not change

context_files:
  - AGENTS.md
  - subsystem/AGENTS.md
  - exact files needed to understand the task

allowed_paths:
  - exact implementation files or narrow directories
readonly_paths:
  - contracts and tests the model may inspect but not edit
forbidden_paths:
  - runtime, secrets, production gates, universes and unrelated systems
  - every path outside the assigned production worktree

input_cutoff:
  trading_date: YYYY-MM-DD
  artifact_hashes: {}

acceptance_commands:
  - deterministic focused test
post_checks:
  - no changes to readonly or forbidden paths
  - no dependency on ashare-pilot-legacy

max_diff_files: 3
max_added_lines: 200
timeout_seconds: 900

expected_handoff:
  - changed paths and behavior
  - commands actually run and exact results
  - diff hash
  - unresolved risks
```

## Delegation Test

A task is suitable for a weak model only when all answers are yes:

1. Is the required behavior already defined by an accepted contract or test?
2. Can one deterministic command objectively accept or reject the result?
3. Can the task be contained within one subsystem and a few files?
4. Can contracts, tests, production runtime, secrets, and deployment remain
   read-only or inaccessible?
5. Would an incorrect implementation fail before reaching production?

If any answer is no, use a strong model or split the task further.

`context_files` is a context-budget tool, not a security boundary. The
dispatcher must separately enforce filesystem and command permissions.
