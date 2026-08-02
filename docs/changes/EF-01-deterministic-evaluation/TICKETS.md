# M1 deterministic evaluation — Implementation Tickets

Contract: `SPEC.md`

## T01 — M1 deterministic safety map and decision log

Requirements:

- `REQ-001`, `REQ-002`, `REQ-003`, `REQ-004`

Scope:

- Add the HC-01 through HC-05A/B Markdown map and the smallest validator check
  for required rows and fully-qualified IDs in the existing discovered suite.
- Add the privacy-minimal Markdown decision log and a synthetic record.
- Reuse the existing DLS test suite; add a regression test only for a
  demonstrated uncovered behavior.

Acceptance:

- The map identifies exact executable behavior-test IDs and hard oracles, the
  validator rejects incomplete or non-discoverable traceability, and the
  existing suite passes.
- The decision log has every required field and no forbidden private content.
- No new runner, JSONL, model call, or lifecycle verdict is introduced.

Validation:

- `python3 plugins/dls/scripts/run_tests.py`
- `python3 scripts/validate_public_repo.py` in a clean public tree
- `python3 -m compileall -q plugins/dls/scripts plugins/dls/hooks scripts`
