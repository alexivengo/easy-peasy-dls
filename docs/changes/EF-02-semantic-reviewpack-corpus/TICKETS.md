# Semantic ReviewPack corpus — Implementation Tickets

Contract: `SPEC.md`

## T01 — Build and exercise the frozen M2 corpus

Requirements:

- `REQ-001`
- `REQ-002`
- `REQ-003`
- `REQ-004`
- `REQ-005`

Scope:

- Create the three fixed-format M2 Markdown artifacts and validate their public
  privacy/grammar boundary with the existing stdlib tooling, including the
  focused P01…P07 positive/negative privacy fixtures.
- Lock and run SR-01, SR-02, SR-03, and SR-04 in order according to the
  release-only runbook with its pinned execution profile after the
  accepted-in-base dependency succeeds, unless its defined hard-gate stop
  creates the terminal executed-prefix path.
- Classify the observed findings through the specified hidden-oracle matcher
  and record one evidence-backed M2 decision.

Acceptance:

- The committed documents have no fabricated live results, raw private data,
  or executable runner/ledger behavior; the focused privacy fixtures reject
  each P01…P07 raw-artifact marker deterministically.
- A completed sample runs all four release-only cases with immutable locks,
  bounded routing/calls, independently replayed arm receipts, and the declared
  hidden-oracle expectations. A valid stop instead accepts the documented
  executed-prefix `aborted`/`not-clear` path with later cases unrun.
- Any incomplete or unsafe case creates the explicit terminal
  `aborted`/`not-clear` record with only its executed prefix; no M2 result
  claims release or production.

Validation:

- Run the repository-owned deterministic commands and focused M2 grammar test.
- Independently review the exact candidate and obtain user acceptance.
- Execute the explicit release-only runbook only after those gates and record
  its outcome separately from DLS acceptance.
