# KANO Attractive backlog — Specification

ID: `KANO-01`

## Problem and outcome

Roadmap records 50 opt-in Attractive candidates A11-A60 with priorities, before-after behavior, promotion signals, and no runtime change.

## Scope

- Add a live-Roadmap section for 50 proposed KANO Attractive capabilities.
- Preserve their IDs, priority group, `Было` problem, and `Будет` outcome.
- State the promotion rules beside the backlog so that it cannot be mistaken
  for an implementation commitment.

## Non-goals

- Implementing any capability in this change.
- Editing `docs/archive/kano-snapshot-2026-07-30.md`; it remains historical
  evidence, not a mutable runtime contract.
- Making any candidate a mandatory delivery gate or a substitute for human
  scope, architecture, or acceptance decisions.
- Adding dependencies, remote services, analytics, global configuration, or
  automatic task creation.

## Current-system discovery

`docs/roadmap.md` is intentionally short and distinguishes `Now`, `Next`,
`Later`, and `Not doing`. The KANO snapshot already owns `A01–A10` and is
frozen. The new section therefore belongs in the live Roadmap as a clearly
non-active candidate catalogue; it must not alter the archived catalogue.

## Requirements and acceptance

- `REQ-001`: Add exactly `A11–A60`, each once, to `docs/roadmap.md`.
- `REQ-002`: Every item has a concise `Было`/`Будет` pair that distinguishes
  the current user problem from the optional delight outcome.
- `REQ-003`: The Roadmap states one promotion rule for each `P1`, `P2`, and
  `P3` group, and caps concurrent `P1` experiments at three.
- `REQ-004`: The section explicitly preserves human decisions, exact-HEAD
  evidence, review boundaries, and the current `Not doing` policy. The new
  section must say that candidates are opt-in, do not create mandatory gates,
  and do not grant automatic approval, task creation, or global configuration
  mutation.
- `REQ-005`: The section makes no claim that a candidate is implemented,
  accepted, released, or production-verified. It must state that the backlog
  is not an active implementation commitment and that review-clear, acceptance,
  release, and production remain separate lifecycle states.

### Required Roadmap promotion text

- `P1`: validate with a real pilot; no more than three P1 experiments may be
  active at once.
- `P2`: promote only after three to five real pilots establish the named user
  problem.
- `P3`: hold until a repeated signal or a real scale need appears.

### Required Roadmap boundary text

- Every candidate is opt-in; none is a mandatory gate.
- The backlog changes neither runtime behavior nor the frozen KANO snapshot.
- It does not create a task, grant an approval, alter global configuration, or
  run delivery autonomously.
- No candidate is claimed implemented, review-clear, accepted, released, or
  production-verified merely by being listed.

## Candidate catalogue

All entries below are KANO Attractive candidates. Their absence must not harm
the delivery workflow; promotion needs the signal named by its priority group.

### P1 — validate with a real pilot

| ID | Было | Будет |
|---|---|---|
| A11 | Receipt is a technical status that must be mentally assembled. | A one-screen card shows HEAD, evidence present and absent, authority, and next action. |
| A12 | An edit can unexpectedly stale an approval or evidence. | Before a commit, a mutation microscope explains exactly what would stale and why. |
| A13 | Requirement-to-proof links are gathered manually. | An evidence atlas links requirement, test, evidence, ReviewIR, and exact HEAD. |
| A14 | Review inputs and lane limits are opaque until it starts. | A flight plan shows inputs, lanes, caps, and review authority before a model call. |
| A15 | Typed stop actions are not always immediately human-readable. | A plain-language explanation gives the reason, preserved work, and shortest permitted step. |
| A16 | Control-level choices feel like policy labels. | A preview compares documents, review, evidence, and human gates for each level. |
| A17 | Missing outcomes, non-goals, or oracles surface late in review. | A light definition doctor reports those omissions before review without a model verdict. |
| A18 | Learning the flow requires touching a real project. | A disposable interactive demo teaches one complete flow and leaves no global state. |
| A19 | Comparing two receipts requires manual reading. | A receipt diff shows changed HEADs, evidence, findings, and lifecycle facts. |
| A20 | Markdown Roadmap and canonical state can silently diverge. | A read-only truth check names the exact discrepancy without rewriting either source. |

### P2 — promote only after three to five real pilots

| ID | Было | Будет |
|---|---|---|
| A21 | A no-op outcome is explained informally. | A certificate proves no source change, candidate, or pretend review claim occurred. |
| A22 | Several worktrees make the owner hard to identify. | An owner compass shows caller, owner, HEAD, and one safe action. |
| A23 | Predictable candidate blockers appear only at handoff. | A dry preflight reports missing contracts, tests, or evidence before execution. |
| A24 | Findings lose context during handoff. | A compact brief keeps risk, location, evidence, and allowed dispositions together. |
| A25 | Remediation order must be reconstructed mentally. | A route map shows current findings and the shortest valid path back to review. |
| A26 | A failed pipeline is represented mostly by logs. | A bounded replay explains a technical failure without another model call. |
| A27 | Exact overlaps and nearby edits are confused. | An overlay distinguishes blocking overlap, advisory proximity, and independent work. |
| A28 | The reviewer input boundary is invisible to the user. | A review window shows the digest-bound context rather than implying whole-repo access. |
| A29 | Budget discussions rely on intuition despite telemetry. | A replay simulates historic runs under another ceiling without changing policy. |
| A30 | Pilot outcomes remain scattered in chats and notes. | A small card records scenario, outcome, negative result, or infrastructure failure. |
| A31 | Implemented is easily mistaken for field-proven or released. | A promise meter separates implementation, test protection, pilot proof, acceptance, and release. |
| A32 | Field evidence can age with the environment. | A freshness view asks for a targeted repeat without invalidating technical receipt evidence. |
| A33 | Reviewer findings are not measured as useful, noisy, or missed. | A privacy-safe ledger accumulates human quality feedback for tuning. |
| A34 | The marginal value of a second lane is asserted, not observed. | A counterfactual viewer reports its demonstrated unique value from real data. |
| A35 | Process cost is debated subjectively. | A bureaucracy budget shows actual minutes, artifacts, and prevented failures per change. |
| A36 | A claim's proof must be searched across artifacts. | A pathfinder leads from a claim to tests, evidence, and explicit holes. |
| A37 | Safe before/after proof is tedious to share. | A user-pasted redacted regression postcard carries the minimum proof. |
| A38 | A single confidence score would overpromise precision. | Evidence bands show distinct test, review, human, field, and release facts. |
| A39 | A policy or skill change is opaque and risky. | A sandbox previews its effects on active changes without writing state. |
| A40 | Plugin upgrades can surprise captured long-running tasks. | An upgrade preview identifies tasks needing restart and compatible captured roots. |

### P3 — hold for a repeated signal or real scale need

| ID | Было | Будет |
|---|---|---|
| A41 | Release gaps live in prose runbooks. | A rehearsal visualizes missing release evidence without blocking code review. |
| A42 | A recipient needs DLS installed to inspect a receipt. | An offline viewer verifies digest and explains a receipt without source access. |
| A43 | Support bundles require hand-scrubbing logs and paths. | An explicit export contains only selected, redacted evidence. |
| A44 | Rollback is a generic document detached from current proof. | A time machine shows possible rollback paths and missing preconditions, never rolling back itself. |
| A45 | Context-size metrics hide what was kept and omitted. | A context-diet view shows inputs, exclusions, limits, and no hidden truncation. |
| A46 | Gates persist from fear rather than evidence. | A detector nominates a data-backed keep, change, or delete audit. |
| A47 | Accepted and dropped changes are buried in Git and state. | A project-local museum makes their provenance browseable without a cross-project dashboard. |
| A48 | Acceptance ends the record even when field outcome is unknown. | A decision echo records whether the promised outcome later appeared. |
| A49 | Review boundaries are learned only on a real change. | A synthetic theatre demonstrates reviewer authority, findings, and human acceptance safely. |
| A50 | Every role handoff is rewritten from scratch. | A dial creates a recipient-specific safe handoff without creating a task. |
| A51 | Incidents rarely become regression cases. | A consented wizard creates a sanitised fixture template for the failure mechanism. |
| A52 | Passing and failing environments are compared by hand. | A redacted fingerprint diff exposes relevant versions and permitted environment facts. |
| A53 | Connector demand is confused with connector value. | A meter recommends a connector only after measured manual handoff savings. |
| A54 | Delivery provenance is fragmented across artifacts. | A storyboard presents ask, proof, review, and decision as one visual line. |
| A55 | Superseded approvals are difficult to understand later. | An approval story explains what changed, when, and the recorded reason. |
| A56 | Simplification starts from a subjective one-off audit. | A deletion radar proposes candidates after enough operational evidence. |
| A57 | Trust boundaries are policy prose rather than a visible system. | A scanner shows what may enter model context and what is isolated. |
| A58 | A file diff does not state user-facing semantic impact. | An optional lens links behavior changes to requirements and labels inference explicitly. |
| A59 | Field-pilot notes are dispersed and inconsistent. | A private local diary records only minimal scenario, outcome, and lesson facts. |
| A60 | A missed problem ends as a vague review failure. | A confidence autopsy identifies the missing evidence or boundary without assigning blame. |

<!-- dls:architecture:start -->
## Architecture and alternatives

No runtime architecture changes. The chosen approach is one Roadmap section
backed by this definition package. Rejected alternatives are changing the
frozen snapshot, creating a new dashboard/database, or implementing any
candidate before a promotion signal.
<!-- dls:architecture:end -->

## Interfaces, state, and failure behavior

Only `docs/roadmap.md` changes in the product tree. The candidate list is
planning text, not DLS state, an executable interface, or a lifecycle claim.
If no promotion signal appears, the correct behavior is to keep the entry
unimplemented.

## Security, privacy, data, and operations

No data collection, connector, remote service, secret, global setting, or
operational mutation is introduced. Several future candidates mention exports
or telemetry, but those remain explicitly deferred ideas.

## UI/UX contract

<!-- dls:design:start -->
Mode: bypass
Rationale: this change edits a Markdown roadmap only; it creates no product UI.
<!-- dls:design:end -->

For a UI change use one committed contract:

```text
Mode: source
Kind: precedent | artifact | external-version
Reference: repository/path-or-https-url
Version: git:<blob-sha> | immutable-external-version
Rationale: why this source governs the change
```

Or explicitly bypass mockups:

```text
Mode: bypass
Rationale: why implementation without a design source is acceptable
```

## Validation intent

- Verify exactly 50 unique `A11–A60` entries, each with `Было` and `Будет`.
- Verify the three required promotion rules, including the P1 maximum of three
  concurrent experiments.
- Verify every required Roadmap boundary statement and the absence of an
  implementation, acceptance, release, or production claim for a candidate.
- Run `python3 scripts/validate_public_repo.py`.

## Risk rationale

Control level: standard.
