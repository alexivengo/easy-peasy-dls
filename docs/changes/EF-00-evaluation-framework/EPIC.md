# Evaluation framework MVP definition — Epic

ID: `EF-00`

## Product outcome

Define a deterministic, privacy-minimal way to decide whether DLS components
make delivery safer, more correct, or cheaper, before adding an eval runtime.

## Scope and deliverables

- A registry of independently meaningful DLS component claims.
- A baseline, hard-gate, failure-taxonomy, privacy, retention, and budget
  contract.
- The architecture boundary for the smallest M1 implementation.
- A self-contained implementation sequence; the separate roadmap is planning
  context, not an input required to interpret this definition.

## Non-goals

- A new evaluator, JSONL ledger, dashboard, database, web service, or DLS state
  schema.
- Live model calls, iOS pilots, a generic MCP evaluator, or Harbor adoption.
- Any claim that definition review, acceptance, release, or production is
  complete before its own gate.

## Success measures

- Every included component has one observable claim and one owner.
- Each planned comparison changes exactly one component and keeps task input,
  permissions, model/effort, and toolchain fixed.
- HC-01 through HC-05 have deterministic oracles; missing values are
  `unknown`, never zero.
- The approved M1 slice needs only existing test infrastructure and one
  Markdown decision log.

## Dependencies

- Explicit definition and architecture approval after independent definition
  review.

## Epic acceptance

- `REQ-001`: The claim registry distinguishes safety from quality, cost, and UX
  claims, and excludes components without an independent consumer.
- `REQ-002`: The baseline and hard-gate contract prevents unsafe or incomparable
  arms from being summarized as a PASS.
- `REQ-003`: Privacy, retention, and budget constraints prohibit raw private
  eval data from canonical DLS state.
- `REQ-004`: M1 uses existing deterministic tests and a Markdown decision log;
  every automation or Harbor step remains trigger-gated.
- `REQ-005`: DLS lifecycle boundaries remain intact: human approval is
  digest-bound, product source has one owner, review remains read-only and
  terminal, and acceptance/release/production stay separate.

## Risk rationale

Control level: standard. The change defines trust, privacy, and evidence
boundaries that govern later implementation but exposes no new runtime surface.
