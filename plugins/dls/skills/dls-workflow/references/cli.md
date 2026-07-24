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
dls review-ready
dls review-start
dls review-import
dls finding set
dls validate
```

Rules:

- Run `--help` for exact arguments; do not guess flags.
- Use `dls adopt` when compatible authored artifacts already exist. Pass repository-relative `--artifact KEY=PATH` values, an explicit `--ticket-status ID=STATUS` for every declared ticket, and any legacy requirement prefixes. Adoption registers state only; it does not rewrite the package. A shared JSON `traceability` artifact is scoped by the adopted ticket IDs so unrelated epic rows do not stale the definition digest.
- Use `--dry-run` before a mutation when practical.
- Read the current revision from `dls status`; pass it as `--expect-revision`.
- Reuse a caller-stable `--operation-id` for retries of the same mutation. Never reuse it for a different mutation.
- Use `--json` when another tool or agent consumes output.
- Keep named command argv/cwd/env/timeout/output caps in `.dls/config.toml`.
- Do not add shell commands to Markdown and do not execute model-authored command strings.
- Evidence summaries must be concise and secret-free. Large raw output belongs only in ignored cache and is bounded by the runner.
- Native review stores the `codex exec review` final message separately from its bounded diagnostic transcript. Transcript truncation is diagnostic metadata, not a review failure; final-result timeout, command failure, absence, oversize, or integrity drift still fail closed.
- `dls worktree register CHANGE_ID /absolute/path` stores local routing under the repository's Git common-dir. Registration requires the same Git repository, a real linked worktree, unchanged branch identity, initialized DLS state, and the named change.
- `dls review-start CHANGE_ID` uses the latest unfinished ReviewPack in the current checkout when that change exists there; otherwise it may resolve only an explicit valid registry binding. It never initializes/adopts, scans sibling directories, or infers branches. An absolute `--pack` remains the explicit one-off cross-checkout selector.
- `dls remediation-start CHANGE_ID` must run on the clean latest reviewed HEAD before source edits. It reads one latest imported ReviewIR and writes one immutable ignored manifest; stale, missing, or tampered review input fails closed.
- `dls review-ready CHANGE_ID --base BASE` is the candidate gateway. It either creates a full/remediation ReviewPack v2 or returns one typed `next_action`. Repeat review must not call raw `review-pack`.
- Implementers use `dls finding set ... addressed` with candidate SHA/evidence. `verified` is unavailable to this command and is created only by independent `review-import`; legacy `resolved` is treated as `addressed`.
- Native review model and effort are fixed inside `review-start`; do not change global Codex model configuration.
