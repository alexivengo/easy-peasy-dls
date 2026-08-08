# bx-dev mechanics adoption — Implementation Tickets

Status: draft; no DLS state, independent review, or human approval.

Contract: `SPEC.md`

## T01 — Closed-world routing registry

Requirements:

- `REQ-001`

Scope:

- Add `plugins/dls/scripts/dls_core/routing.py` with frozen collections
  for review verdicts, runner states, stream events, and `next_action.id`.
- Replace direct string literals used as routing keys in `runner.py`,
  `core.py`, CLI, skill, and hooks with references to the registry.
- Do not change the public CLI surface, state schema, ReviewPack, or
  ReviewIR schema.

Acceptance:

- The four families are exposed as named symbols; no consumer matches a
  routing value by string literal.
- A repository grep for the previous string keys returns only the
  registry itself.
- `docs/technical-reference.md` cross-references the registry module.

Validation:

- Run the closed-world tests in `T04`.
- Run the existing test suite.

## T02 — Unknown routing value fail-closed

Requirements:

- `REQ-002`

Scope:

- Add `route_unknown(field, value)` helper that raises
  `DlsIntegrityFailure` with the shape from `SPEC.md`.
- Wire the helper into every consumer that previously string-matched a
  routing value.
- Render the typed diagnostic in CLI and skill; do not display the raw
  model output.

Acceptance:

- Unknown review verdict, runner state, stream event, or `next_action.id`
  produces the JSON shape from `REQ-002`.
- DLS state is unchanged on failure; no destructive Git action runs.
- The diagnostic carries the field name, offending value, and the
  recognized list.

Validation:

- Run the unknown-value tests in `T04`.
- Confirm `git status --porcelain` is unchanged after each forced
  failure.

## T03 — Risk-trigger rationale

Requirements:

- `REQ-003`

Scope:

- Document the included, intentionally excluded, and reason lists in
  `docs/technical-reference.md` under a new `Risk trigger rationale`
  subsection.
- Mirror the same lists in the `runner.py` module header as a docstring.
- Do not touch `assets/profiles/*.toml`.

Acceptance:

- The three lists in `docs/technical-reference.md` and the runner header
  match byte-for-byte.
- Profiles remain advisory and do not gain rationale, gates, models, or
  budgets.

Validation:

- Run the positive and near-miss negative trigger tests in `T04`.

## T04 — Reversibility invariant

Requirements:

- `REQ-004`

Scope:

- Add one paragraph to `docs/technical-reference.md` stating the
  `reversible → gate → irreversible` invariant.
- Add one regression test under `plugins/dls/tests/` that asserts an
  irreversible adapter cannot run before the human gate.
- Add a second test that asserts a post-gate failure preserves state and
  evidence and does not run `git reset`, `git stash`, or `git revert`
  automatically.

Acceptance:

- The invariant paragraph is present and unchanged in
  `docs/technical-reference.md`.
- Both regression tests pass.
- No new dependency, secret, or remote call is introduced.

Validation:

- Run the new tests in isolation and as part of the full suite.

## T05 — Test pack for the change

Requirements:

- `REQ-001` through `REQ-005`

Scope:

- One test per row below, located under `plugins/dls/tests/` and named
  after the bullet:

  1. `test_unknown_review_verdict_is_integrity_failure`
     - feed an unknown verdict to the runner;
     - assert `DlsIntegrityFailure` with `error="unknown-routing-value"`;
     - assert state path is unchanged.
  2. `test_unknown_next_action_id_stops_orchestration`
     - feed an unknown `next_action.id` from a skill or hook;
     - assert the orchestration stops, no fallback action runs, and the
       diagnostic includes the recognized list.
  3. `test_risk_trigger_positive_cases`
     - for each included trigger, assert the expected secondary reviewer
       lens, model, and effort.
  4. `test_risk_trigger_near_miss_negatives`
     - for `architecture`, `release`, `external-dependency`, assert no
       secondary reviewer is allocated.
  5. `test_irreversible_adapter_before_human_gate_is_rejected`
     - call an irreversible adapter before the human gate;
     - assert the call raises and no external side effect occurred.
  6. `test_post_gate_failure_preserves_state_and_evidence`
     - simulate a post-gate failure;
     - assert DLS state and ReviewPack/ReviewIR evidence are unchanged;
     - assert no `git reset`, `git stash`, or `git revert` ran.

- One repo-wide guard:

  7. `test_no_bx_dev_anti_patterns`
     - grep the change diff for the forbidden list in `REQ-005`;
     - assert zero hits.

Acceptance:

- All seven tests pass.
- The test module is registered in the existing test runner entry point.

Validation:

- Run the test module under the existing pytest harness.

## Forbidden-list reference for `T05` test 7

```text
session-branch, --solo, --careful, --plan-approve, --no-review,
--sop, --no-sop, scout-report, type(dev), marketing/, webapp-testing,
audit-website, DDD, bounded-context, aggregate root
```
