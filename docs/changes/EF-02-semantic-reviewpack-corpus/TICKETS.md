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
  privacy/grammar boundary with the existing stdlib tooling.
- Lock and run exactly SR-01, SR-02, SR-03, and SR-04 according to the
  release-only runbook after the accepted-in-base dependency succeeds.
- Classify the observed findings through the specified hidden-oracle matcher
  and record one evidence-backed M2 decision.

Acceptance:

- The committed documents have no fabricated live results, raw private data,
  or executable runner/ledger behavior.
- All four release-only cases have immutable locks, stay within their routing
  and call bounds, and meet their hidden-oracle expectations.
- Any incomplete or unsafe case keeps M2 `not-clear`; no M2 result claims
  release or production.

Validation:

- Run the repository-owned deterministic commands and focused M2 grammar test.
- Independently review the exact candidate and obtain user acceptance.
- Execute the explicit release-only runbook only after those gates and record
  its outcome separately from DLS acceptance.
