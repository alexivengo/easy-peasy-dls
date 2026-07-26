# DLS CLI usage

Resolve the plugin root from this skill's installed location and invoke:

```text
python3 <plugin-root>/scripts/dls.py --root <repo> [--json] <command>
```

Core commands:

```text
dls init
dls doctor
dls new
dls adopt
dls worktree register|list|verify|unregister
dls status
dls check
dls context
dls approve
dls ticket set
dls evidence add
dls review-pack
dls remediation-start
dls remediation-recover
dls candidate-ready
dls candidate-status
dls review-ready
dls review-start
dls review-run
dls review-status
dls review-import
dls finding set
dls validate
```

Rules:

- Run `--help` for exact arguments; do not guess flags.
- Use `dls adopt` when compatible authored artifacts already exist. Pass repository-relative `--artifact KEY=PATH` values, an explicit `--ticket-status ID=STATUS` for every declared ticket, and any legacy requirement prefixes. Adoption registers state only; it does not rewrite the package. A shared JSON `traceability` artifact is scoped by the adopted ticket IDs so unrelated epic rows do not stale the definition digest.
- Use `--dry-run` before a mutation when practical.
- Low-level mutations require the current revision from `dls status`; `candidate-ready` owns its own state transitions and does not accept one.
- Reuse a caller-stable `--operation-id` for retries of the same mutation. Never reuse it for a different mutation.
- Use `--json` when another tool or agent consumes output.
- Keep named command argv/cwd/env/timeout/output caps in `.dls/config.toml`.
- Do not add shell commands to Markdown and do not execute model-authored command strings.
- Evidence summaries must be concise and secret-free. Large raw output belongs only in ignored cache and is bounded by the runner.
- Resolve this CLI from the loaded skill location as `<plugin-root>/scripts/dls.py`. Never probe `PATH` for `dls`.
- Review model runs store the bounded final structured result separately from the JSONL diagnostic transcript. Transcript truncation is diagnostic metadata, not a review failure.
- `dls worktree register CHANGE_ID /absolute/path` stores local routing under the repository's Git common-dir. Registration requires the same Git repository, a real linked worktree, unchanged branch identity, initialized DLS state, and the named change.
- `dls review-start CHANGE_ID` uses the latest unfinished ReviewPack in the current checkout when that change exists there; otherwise it may resolve only an explicit valid registry binding. It never initializes/adopts, scans sibling directories, or infers branches. An absolute `--pack` remains the explicit one-off cross-checkout selector.
- `dls remediation-start CHANGE_ID` verifies and returns the canonical manifest created atomically with the latest actionable ReviewIR. It is read-only and may route to a registered owner worktree.
- `dls remediation-recover CHANGE_ID [--review-id ID]` is the explicit legacy-only repair path. It validates exact Git objects, ancestry, definition, ReviewPack and ReviewIR digests without switching the checkout, then creates the missing canonical manifest.
- `dls candidate-ready CHANGE_ID [--base BASE] [--address ID] [--note ID] [--extra-command ID]` is the implementation-side orchestration command. It requires explicit `policy.review_required_commands`, reuses only exact-contract evidence, runs trusted commands sequentially, records dispositions, and atomically creates the ReviewPack. The first review requires `--base`; remediation infers it. The skill invokes it after the candidate commit; the user does not.
- `dls candidate-status CHANGE_ID` is compact read-only telemetry and never runs a command.
- `dls review-ready CHANGE_ID [--base BASE]` is the candidate gateway. The first review requires `--base`; remediation infers it from the latest ReviewIR. It either creates a full/remediation ReviewPack v2 and returns `open-review-task`, or returns one typed `next_action`. Repeat review must not call raw `review-pack`.
- `dls review-run CHANGE_ID` is the public review orchestration command. It selects or prepares the exact-HEAD pack, runs every mandatory lane with single-flight protection, creates ReviewIR, and imports it. A completed `not-clear` verdict exits successfully; nonzero exit is reserved for infrastructure or integrity failure.
- `dls review-status CHANGE_ID [--review-id ID]` is read-only and compact by
  default. It never launches a model. `--verbose` adds full argv, paths, and
  provenance only for diagnostics.
- `dls review-start CHANGE_ID` remains a native-only compatibility and diagnostic primitive.
- Implementation/remediation tasks call only `candidate-ready` after commit and stop at `open-review-task`; only a separate explicit review task invokes `review-run`.
- `finding set`, `validate`, `evidence add`, and `review-ready` remain low-level diagnostics and compatibility primitives. Normal implementation/remediation uses `candidate-ready`, which binds all required successful evidence to each addressed finding. `verified` remains available only through independent `review-import`; legacy `resolved` is treated as `addressed`.
- Native review model and effort are fixed inside `review-start`; do not change global Codex model configuration.
