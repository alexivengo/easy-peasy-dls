# Technical reference — v0.14

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
Every status response includes a bounded `platform_profile` projection with its
contract, name, digest and at most 16 advisory capabilities and skills. It never
contains profile source paths or repository discovery output. Review metrics use
the profile provenance stored with that exact canonical result rather than the
current repository profile.

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

The optional derived `presentation` does not participate in the decision ID. It
adds localized labels, short digests and the effect of `Да`; `Нет` explicitly
leaves state unchanged. The acceptance presentation states that acceptance is
bound to the reviewed HEAD and does not establish release or production.

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

The RUCORE-E03 pilot exposed a narrower invalid-output case: a reviewer copied
authored ticket lifecycle `blocked` into semantic ticket verdicts while its
global decision was `review-clear`. The validator correctly rejected those
rows, but the old compact repair prompt did not distinguish lifecycle status
from review verdict, and its failed attempt became sticky. Repair contract v2
makes that distinction explicit and permits exactly one new compact repair for
a failed legacy repair contract. It reuses the immutable primary output and
never repeats repository analysis.

The same pilot then exposed a chained cross-field case: a `not-clear` decision
combined lifecycle-derived blocked ticket rows with an existing should-fix that
blocked acceptance/release but omitted `review`. Fail-fast validation reported
only the ticket-row error, so repair v2 could reveal and then fail on the second
constraint. Repair contract v3 states all cross-field rules up front: it clears
unsupported lifecycle rows and may add only the missing `review` block to an
already actionable finding required by the preserved global verdict. It still
cannot invent a finding, change its semantic content or repeat source analysis.

ReviewIR v3 and the corresponding current review record optionally carry
`platform_profile {contract, name, digest}` copied from the digest-bound
ReviewPack. Results without this additive provenance remain readable.

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

An interrupted dirty owner remains protected by default. For the unique
`commit-owner-source` boundary the workflow asks once whether to continue the
existing draft. An immediate `Да` authorizes preserving and extending that diff
in the owner; it never authorizes reset, stash, overwrite, transfer or merge.
Ambiguous, divergent and cross-repository owners remain hard blockers.

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

`v0.13.3` adds an explicit one-question recovery handoff for an interrupted
uncommitted owner draft without weakening the default worktree safety rule.

### v0.13.4 runtime completion guard

`v0.13.1` fixed the candidate resolver, while `v0.13.2` and `v0.13.3` made the
non-terminal loop explicit in the skill. EPIC-03a still stopped after a clean
checkpoint because those instructions were advisory: `continue-implementation`
existed in JSON, but nothing intercepted the Codex `Stop` event. Text-level
forward tests therefore passed without exercising turn completion.

The plugin now bundles `UserPromptSubmit` and `Stop` hooks. An explicit single-
change implementation/remediation prompt creates a private binding in
`PLUGIN_DATA` under a SHA-256 session key. On a non-terminal DLS action the Stop
hook returns `decision: block` with a short `[DLS_CONTINUE]` prompt. It never
stores a raw session ID, transcript or repository path, never changes DLS state
or product source. A guard failure is fail-open and visible.

The skill still describes the desired behavior; the hook enforces the runtime
boundary. Human decisions, `open-review-task`, dependency/workspace conflicts
and integrity failures remain terminal. Plugin hooks are trusted separately by
Codex: inspect and trust the exact definition once through `/hooks` after an
install or hook update.

### v0.13.5 consent and progress continuity

Two live EPIC-03a turns exposed gaps in the first guard contract. A task that
created its own dirty draft received the same `commit-owner-source` boundary as
a pre-existing user draft. After the required question, the short answer `Да`
did not look like a new implementation command, so `UserPromptSubmit` cleared
the binding and the following turn ran without Stop protection.

The guard now records only safe private aggregates: whether the owner was dirty
at activation, an approval state, HEAD and a SHA-256 Git progress fingerprint.
A pre-existing draft enters `awaiting-owner-consent`; exact `Да` revalidates the
same unchanged draft and rearms the guard, while `Нет`, cancellation or drift
clears it. A draft created by the active task is already authorized by the
implementation request and remains non-terminal. This release counted only
consecutive stops without Git activity. A later live remediation proved that
edits and reverts could reset the counter indefinitely.

### v0.13.6 bounded and upgrade-safe runtime guard

The EPIC-03a remediation received 17 automatic continuations while oscillating
between patches. The root cause was using a Git fingerprint as proof of semantic
progress: every edit, revert, commit or state change reset the guard even when
the actionable finding and failing tests did not improve.

The v3 private guard contract removes progress scoring. Each explicit
implementation/remediation prompt receives at most two automatic continuations.
The third premature Stop clears the binding and returns
`continue:false`, `dls-auto-continuation-exhausted` and a bounded diagnostic;
it does not spend another model call. Hook-generated prompts never rearm the
guard.

Draft consent remains exact but is now bound to hashes of the Git common-dir,
owner gitdir, HEAD and tracked/staged/untracked content. Snapshot collection has
one deadline and output limits. A changed identity or draft requires new
consent. Private bindings expire after 24 hours; legacy/corrupt bindings and
guard failures are cleared fail-open.

Codex snapshots a hook definition for an open task. Previously that definition
directly executed a script under versioned `PLUGIN_ROOT`; removing the old cache
made the hook itself impossible to start. `hooks.json` now contains a tiny
inline bootstrap. It executes the exact captured plugin-local script when
present. When the versioned root has gone, it emits
`dls-hook-upgrade-required` and exits successfully without blocking Stop or
searching PATH/source/latest cache. A plugin update still requires a Codex
restart and a fresh task; the bootstrap makes this failure finite and explicit,
not hot-reloadable.

### v0.14.0 platform proof and decision UX

The workflow reads the resolved profile from its first status call. Domain
skills are advisory: only installed and relevant skills are used, and their
absence never blocks delivery. Apple UI work may use architecture, concurrency,
testing, design and the universal Swift accessibility companion. Backend work
does not inherit Apple UI, App Store or Apple-platform gates; Swift guidance may
still apply to Swift server source.

The release is proof-first: Apple UI definition/design, backend
definition/architecture and a full backend review/acceptance lifecycle must
complete as real pilots before the corresponding roadmap items are closed.
Models, review routing and budgets are intentionally unchanged.

The Apple pilot found that a shared traceability artifact was previously scanned
as unscoped text. Requirement projection now honors its state-owned
`producer_ticket_scope`; IDs owned by other changes cannot enter the ReviewPack.
