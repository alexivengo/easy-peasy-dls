# M1 deterministic evaluation — Implementation Tickets

Contract: `SPEC.md`

## T01 — M1 deterministic safety map and decision log

Requirements:

- `REQ-001`, `REQ-002`, `REQ-003`, `REQ-004`

Scope:

- Add the HC-01 through HC-05A/B Markdown map and the smallest validator check
  for required rows and fully-qualified IDs in the existing discovered suite.
- Add the closed privacy-minimal Markdown seed log and its fixed synthetic
  record.
- Strengthen the demonstrated HC-01 and HC-04 hard-oracle tests, add one
  isolated validator-contract test, and add the dependency-definition-drift
  regression. Add one clean-archive `codex`-sentinel regression proving the
  three L0 commands do not invoke a live model. Reuse the existing DLS test
  suite for all of them.

Acceptance:

- The map identifies exact executable behavior-test IDs and hard oracles, the
  validator rejects incomplete or non-discoverable traceability, and the
  existing suite passes.
- The validator rejects any departure from the closed seed log, including
  missing fields, altered synthetic values, and injected forbidden content; its
  focused positive and negative test passes.
- An acceptance recorded against an old target definition cannot unlock a
  dependent candidate.
- The three L0 commands pass behind the failing supported-live-`codex`
  sentinel without invoking it; existing temporary fake-Codex tests remain
  allowed doubles.
- No new runner, JSONL, model call, or lifecycle verdict is introduced.

Validation:

- `python3 plugins/dls/scripts/run_tests.py`
- `python3 scripts/validate_public_repo.py` in a clean public tree
- `python3 -m compileall -q plugins/dls/scripts plugins/dls/hooks scripts`
