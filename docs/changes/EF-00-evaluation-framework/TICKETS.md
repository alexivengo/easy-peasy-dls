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
  and architecture.

Validation:

- The repository-owned test, public-validator, and compile checks pass.

## T02 — M1 deterministic safety map (follow-up change)

Blocked by: EF-00 definition and architecture approval.

Scope: map HC-01 through HC-05 to current tests and add only demonstrated
missing deterministic regression proofs.

## T03 — M1 decision log (follow-up change)

Blocked by: EF-00 definition and architecture approval.

Scope: add one privacy-minimal Markdown decision log; do not add JSONL.

## T04 — M2 semantic corpus (follow-up change)

Blocked by: M1 exit gate.

Scope: four frozen release-only cases with the exact hard-oracle and
classification contract from `SPEC.md`.

## T05 — Automation and Harbor (deferred)

Blocked by: the documented automation or scale trigger. No implementation is
allowed merely because the backlog exists.
