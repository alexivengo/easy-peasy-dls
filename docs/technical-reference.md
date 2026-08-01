# Technical reference — v0.13

## Public CLI

```text
init
doctor
new
adopt
upgrade
status
approve
ticket
dependency
candidate-ready
review-run
worktree prepare
```

`status CHANGE_ID --details findings|receipt|metrics|history` replaces the old
candidate/review/delivery status, receipt, metrics, history and cache surfaces.

`review-run CHANGE_ID --kind definition|code --stream` is the only public review
orchestration command. No public command accepts an operation ID, state revision,
pack path, result path, evidence path, raw argv, or recovery mode.

## State and artifacts

- current persistent state: `schema_version: 2`;
- current ReviewPack and ReviewIR: `schema_version: 3`;
- model decision schema: `review-decision.schema.json`;
- legacy state is accepted only by `upgrade`;
- legacy ReviewPack/ReviewIR remains immutable archive evidence and is never run.

State contains current change metadata, authored sources, approvals/revocations,
tickets, accepted-in-base dependencies, candidate, latest definition/code review,
current findings, acceptance, and one active run. It contains no operation
history, task/thread IDs, controller rollout telemetry, raw prompt/transcript,
or absolute worktree path.

## Decision gates

Definition, architecture and design approvals are independent digest-scoped
records. They may share one atomic bundle. Architecture is derived from a
committed ADR or bounded SPEC architecture section. UI design uses one exact
precedent/artifact, immutable external version, or explicit bypass rationale.

Human prompts use `dls-human-decision/v1`. Its deterministic ID binds the
change, action, current HEAD, review and decision digests. The user answers
`Да` or `Нет`; `approve --decision-id` recomputes the card before the atomic
write. A stale card, negative answer or mismatched bundle changes no state.
Legacy direct CLI calls may still repeat explicit digests/SHA, but the workflow
never asks a person to copy them.

Standard/critical definition approval requires a current clear
`review-run --kind definition`. Changing authored definition or a scoped
decision makes the corresponding approval stale. Generated DLS state and
validation evidence do not change the authored digest.

## Candidate

`candidate-ready` requires:

- clean committed product source;
- current required decisions;
- satisfied accepted-in-base dependencies;
- implemented/validated/done tickets;
- explicit `policy.review_required_commands`.

Commands are repository-owned fixed argv arrays. They run sequentially with
bounded timeout/output. Successful evidence stores only metadata/digests and is
bound to HEAD, product tree and command contract. Failure stores one bounded
latest diagnostic under ignored cache. A successful candidate creates one
ReviewPack v3.

The initial candidate base is immutable across exact-HEAD retries and descendant
candidate corrections. `candidate-ready` reuses the preserved base and rejects
a conflicting replacement. Migration restores that base from its digest-checked
pre-0.11 backup before rebuilding a ReviewPack.

## Review runner

Every analysis uses `codex exec` with:

- clean detached exact-HEAD worktree;
- read-only sandbox;
- ephemeral session and ignored user config/rules;
- fixed model/effort;
- strict output schema;
- bounded streaming, timeout and transcript;
- process-group termination.

Routing:

| Path | Primary | Optional secondary | Aggregate |
|---|---:|---:|---:|
| routine | Terra/high 650k | — | 750k |
| standard | Terra/high 1.25M | — | 1.5M |
| critical | Terra/high 1.25M | Sol 1M by risk | 3M |

Critical secondary triggers:

- auth/security-privacy → trust/xhigh;
- data-loss/data-migration → data/xhigh;
- concurrency/availability → reliability/xhigh;
- public-api/compatibility → contract/high.

Architecture, release and external-dependency alone do not add a reviewer.
Any primary blocker or should-fix is sufficient to import `not-clear`; optional
secondary and reconciliation are skipped. A second reviewer is required only
before potential critical `review-clear`. Additive secondary findings merge
conservatively and do not trigger reconciliation. Reconciliation runs only for
a direct prior-finding or shared-finding classification contradiction. It sees
structured outputs and compact pack, not the product checkout.

Lane token values are allocations. A completed structured result records actual
usage and an `over_target` warning instead of being discarded. Aggregate
overrun can never create `review-clear`; an already valid actionable decision
may still be imported as safe `not-clear`, because the spend has already
occurred and no clearance is granted. Timeout, transcript and process-group
limits remain terminal.

Logical invalid output is never re-analysed. One compact Sol repair sees only
the raw decision, validation error and permitted identifiers. Transport may
retry once. Content-derived run IDs and a crash-safe flock provide single-flight.
Completed valid lanes are reused during deterministic finalization.
ReviewIR routing provenance records planned, completed, skipped and recovered
lanes. A pre-v0.13 failed run with an exact, digest-valid actionable primary can
be finalized without another model call.

Public runner states are `not-prepared`, `running`, `completed`, `blocked` and
`failed`. `started` and `lane-transition` stream events are non-terminal; only a
`completed` event with `terminal=true` ends the owning process. A failed review
returns a typed inspection action and is never projected as `open-review-task`.
A canonical completion always returns `review_result_path`.

## Findings and lifecycle

Implementation owns only `addressed` and `note`. A human may explicitly waive.
Independent ReviewIR owns `verified`, `still-open` and `regressed`.

A clean commit inside a `not-clear` remediation is only a checkpoint while any
current blocker or should-fix for review/acceptance lacks a current-HEAD
`addressed` or `note` disposition. Status remains `continue-implementation`;
the workflow invokes `candidate-ready` once after the complete remediation.
`note` requests independent adjudication and is never a shortcut for unfinished
work. Waived and release/production-only findings do not hold code handoff.

Implementation actions `continue-implementation`, `remediate-findings`,
`run-candidate-ready`, `fix-validation`, and `wait-candidate` are non-terminal.
The workflow continues after checkpoints without another user prompt. It ends
only at `open-review-task`, a human decision, an external conflict, or a proven
integrity/infrastructure blocker. Already-open Codex tasks do not hot-reload an
updated plugin skill and must be replaced after reinstall.

`review-clear`, `accepted`, `release` and `production` are separate. Receipt is
a deterministic read-only projection available through status; it creates no
artifact and performs no model call.

## Dependencies and worktrees

A dependency has one meaning:

```text
implementation requires OTHER_CHANGE accepted-in-base
```

The target must be accepted and its reviewed HEAD must be an ancestor of the
dependent HEAD. Cycles and cross-repository targets are rejected.

Git `worktree list --porcelain` is authoritative. The common-dir registry stores
only `change_id → gitdir identity`; path, branch-name inference, sibling scanning,
transfer journals and manual register/unregister lifecycle do not exist.
Prunable Git entries are ignored and cannot break routing to a live owner.

Lifecycle JSON includes local-only `dls-execution-context/v1` with caller,
resolved owner, exact owner HEAD, dirty flags and one workspace action. It is
never written to state, ReviewPack or ReviewIR. `worktree prepare` may derive
its base from committed caller HEAD. A uniquely matching Git-known worktree can
restore a missing registry binding; branch name is not identity. Ambiguous,
dirty-owner and divergent cases stop without stash/reset/transfer.

## Migration

`upgrade --dry-run` validates every legacy reference before writing.
`upgrade --apply` creates ignored `.dls/archive/pre-0.11/`, projects current
approvals/tickets/dependencies/candidate/latest review/findings/acceptance, writes
state v2 atomically and creates the compact worktree identity registry.

Mixed v1/v2 repositories return `upgrade-incomplete`. Re-running upgrade is
idempotent. Rollback restores the archived state and reinstalls v0.10.2; product
Git history is never changed.

`v0.11.2` restores a migrated candidate's original review base from the
digest-checked archive, invalidates a pack built from a conflicting base, and
requires one fresh `candidate-ready` before review. It also makes failed review
status and stream termination explicit.

`v0.11.3` fixes the Codex App orchestration boundary for long reviews. The
workflow keeps the wrapper cell alive and polls the nested `exec_command`
session with `write_stdin`; an outer cell completion can no longer be mistaken
for review completion while DLS continues in the background.

`v0.12.0` introduces one-reply decision cards for approval and acceptance.

`v0.13.0` adds owner-first execution routing and budget-safe early `not-clear`.

`v0.13.1` prevents a clean intermediate remediation commit from becoming a
partial review candidate.

`v0.13.2` makes the implementation loop explicitly non-terminal until its DLS
handoff or a real external blocker.
