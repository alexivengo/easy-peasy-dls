# Evaluation framework MVP definition — Implementation Tickets

Contract: `SPEC.md`

## T01 — Commit the approved M0 definition

Requirements:

- `REQ-001`
- `REQ-002`
- `REQ-003`
- `REQ-004`
- `REQ-005`

Scope:

- Keep the claim registry, baseline, gates, taxonomy, privacy/retention,
  budgets, and M1 boundary in the committed definition artifacts.

Acceptance:

- Definition review clears the exact digest and the user approves definition
  and architecture; the M0 diff changes only definition/roadmap documentation,
  not `plugins/dls` runtime source, hooks, state schema, or public CLI.

Validation:

- The repository-owned test, public-validator, and compile checks pass.

## T02 — M1 deterministic safety map (follow-up change)

Blocked by: EF-00 definition and architecture approval.

Requirements: `REQ-001`, `REQ-002`, `REQ-004`, `REQ-005`.

Scope: map HC-01 through HC-05 to current tests and add only demonstrated
missing deterministic regression proofs.

Acceptance: every hard claim links to its exact test symbol and oracle; any
new test fails before its protected behavior and passes after it; no model call
is introduced into the deterministic suite. HC-06 through HC-08 are excluded
from this M1 ticket and remain post-MVP work. HC-05 passes only when both
HC-05A and HC-05B are mapped and pass.

Validation: `python3 plugins/dls/scripts/run_tests.py`,
`python3 scripts/validate_public_repo.py`, and `python3 -m compileall -q
plugins/dls/scripts plugins/dls/hooks`.

## T03 — M1 decision log (follow-up change)

Blocked by: EF-00 definition and architecture approval.

Scope: add one privacy-minimal Markdown decision log; do not add JSONL.

Requirements: `REQ-003`, `REQ-004`.

Acceptance: records contain the contract fields, arm-manifest digest, and
retention disposition but no raw private content or path; deleting a raw local
artifact leaves its receipt-reference rule intact.

Validation: public validator plus a documented synthetic record review.

## M1 exit gate

M2 may start only after the DLS change delivering T02 and T03 is accepted. Its
receipt must bind the exact claim-to-test map, successful deterministic suite
and compile/public-validator evidence, and one synthetic decision-log record
that passes T03 privacy checks. The DLS maintainer records `M1-exit` with those
receipt digests in the decision log; otherwise M2 remains blocked.

## T04 — M2 semantic corpus (follow-up change)

Blocked by: M1 exit gate.

Scope: four frozen release-only cases with the exact hard-oracle and
classification contract from `SPEC.md`.

Requirements: `REQ-001`, `REQ-002`, `REQ-005`.

Acceptance: every case has a versioned oracle and arm manifest; component-off
cases alter one component only; previous-release cases are labeled regression
only and Native Codex cases overall-overhead only; each case declares its
expected reviewer verdict and applicability; all four M2 cases pass individually,
while 19/20 and false-block thresholds wait for the rolling 20-case window;
infrastructure failures cannot be reported as product results. The runbook
records the four-step manual manifest/hard-oracle checklist from `SPEC.md` and
one synthetic invalid-manifest record that is excluded from aggregation. A
causal component-off case additionally uses a registry component ID, declared
fixture/config switch, on/off configuration digests, and clean-copy HEADs.
The M2 lock supplies every field of the `SR-01`…`SR-04` matrix before its first
live call; a placeholder is not accepted.

Validation: full L0 suite, four-case semantic release run, and budget/decision
log review.

## T05 — Automation and Harbor (deferred)

Blocked by: the documented automation or scale trigger. No implementation is
allowed merely because the backlog exists.

Requirements: `REQ-004`.

Acceptance: the recorded trigger identifies two over-30-minute manual release
evaluations, more than four live cases, one case decision requiring more than
two arms, two transcription errors, or required machine-readable interchange;
the proposed automation keeps the arm-manifest and privacy contracts.

Validation: deterministic self-checks, repeatable output check, and public
validator. Harbor additionally requires its separate M2 scale, privacy, and
container-scope decision.
