# M1 deterministic evaluation — Epic

ID: `EF-01`

## Product outcome

An executable, zero-model-call L0 safety map for HC-01 through HC-05A/B and
one privacy-minimal Markdown decision log.

## Scope and deliverables

- `docs/evaluation-claim-map.md` maps every M1 hard claim to existing
  `unittest` symbols and its deterministic oracle.
- The existing public validator rejects an incomplete map or a map that names a
  missing/non-discoverable test ID or an invalid synthetic log record; the
  existing `run_tests.py` remains the only test runner.
- `docs/evaluation-decisions.md` supplies one small, privacy-minimal Markdown
  record format and a synthetic M1 record. It does not add JSONL or DLS state.
- The synthetic M1 record is format evidence only. M1 exit is enforced by the
  existing DLS `accepted-in-base` dependency; a later M2 definition may record
  the EF-01 acceptance evidence and receives its own independent review.
- Strengthen only the demonstrated HC-01, HC-04, and dependency-digest test
  gaps in the existing standard-library suite.

## Non-goals

- A new evaluator/runner, JSONL ledger, dashboard, database, service, or DLS
  state schema.
- Live model calls, semantic fixtures, component ablations, iOS observations,
  CI/release automation, or Harbor adoption.
- HC-06 through HC-08, which remain post-MVP work.
- Any release or production claim.

## Success measures

- Every HC-01 through HC-05A/B row names an exact current test and deterministic
  oracle; an absent required row or test symbol fails the public validator.
- A stale/negative decision preserves the state digest; canonical review
  completion includes both `terminal=true` and a result path; an old acceptance
  digest cannot satisfy a newer dependency.
- The existing deterministic suite passes with zero model calls.
- The log has the approved decision fields while excluding paths, raw
  transcripts, source, and secrets.

## Dependencies

- `EF-00` — accepted in base.

## Epic acceptance

- `REQ-001`: Every M1 hard claim is traceable to an exact executable test and
  deterministic oracle.
- `REQ-002`: The fast public check rejects a missing required claim or missing
  mapped test ID, unsafe synthetic decision-log record, or missing required
  decision-log field; behavior remains proven by the existing full suite.
- `REQ-003`: The decision log is Markdown-only, privacy-minimal, and contains a
  synthetic record without paths, raw transcripts, source, or secrets.
- `REQ-004`: M1 checks make zero model calls and cannot authorize M2, release,
  or production. Only DLS's accepted-in-base dependency can unlock M2
  implementation, and it requires the current acceptance digest to match the
  dependency target; a copied receipt is audit evidence, never a gate.

## Risk rationale

Control level: standard. This change extends the evidence contract used for
future safety and privacy decisions, but it introduces no new runtime surface.
