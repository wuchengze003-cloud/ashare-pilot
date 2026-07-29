# Operations Rules

`ops/` orchestrates bounded jobs, verification, deployment, rollback, and
operational evidence. It must not contain strategy or portfolio semantics.

## Rules

1. Every coding job starts from an exact base commit in an isolated worktree.
2. Allowed, read-only, and forbidden paths are machine-enforced. Prompts and
   `context_files` reduce context but do not provide access control.
3. Read-only jobs require filesystem protection for tracked files, ignored
   runtime, credentials, and secrets. Post-run Git status alone is insufficient.
4. Agent tests are reviewed commands, not arbitrary shell supplied by an
   untrusted model.
5. A job cannot promote a model, change a universe, alter production runtime,
   deploy, or merge unless the manifest type and human approval explicitly
   permit that operation.
6. Data refresh, inference, validation, build, and deployment are separate
   stages with separate evidence. A single agent prompt must not silently own
   all stages.
7. Deployment consumes a reviewed commit and immutable runtime manifest. It
   does not run parameter search, strategy selection, or source edits.
8. Job output records base SHA, diff hash, changed paths, tests, artifact
   hashes, verdict, duration, and reviewer disposition.
9. Production jobs may not mount, read, import, deploy, or execute the Legacy
   repository. Legacy archival is a separate human-approved maintenance task.

Use [`agents/TASK_TEMPLATE.md`](agents/TASK_TEMPLATE.md) for delegated work.
