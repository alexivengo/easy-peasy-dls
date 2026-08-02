# M1 deterministic evaluation — Specification

ID: `EF-01`

## Problem and outcome

L0 safety behavior already has focused `unittest` coverage, but an operator
cannot see which exact test proves each hard claim. The first executable slice
must make that mapping auditable without creating a parallel evaluation
platform or collecting private evidence.

EF-01 adds the map, makes its completeness a public-validator check, and adds
the one Markdown decision log required by EF-00. `run_tests.py` remains the
single suite executor; no model call is part of this change.

## Scope

- Add `docs/evaluation-claim-map.md` with these M1 rows and exact current test
  symbols:

  | Claim | Required proof families |
  |---|---|
  | HC-01 decision integrity | `test_core_reset_v011.CoreResetTests.test_acceptance_is_separate_and_exact_head`, `test_core_reset_v011.CoreResetTests.test_stale_human_decision_cannot_accept_new_head` |
  | HC-02 owner safety | `test_core_reset_v011.CoreResetTests.test_execution_context_prepares_owner_and_leaves_dirty_caller_untouched`, `test_core_reset_v011.CoreResetTests.test_dirty_main_routes_candidate_and_review_to_clean_owner`, `test_core_reset_v011.CoreResetTests.test_dirty_owner_stops_before_product_work`, `test_core_reset_v011.CoreResetTests.test_second_state_bearing_owner_is_an_explicit_conflict` |
  | HC-03 exact evidence | `test_core_reset_v011.CoreResetTests.test_exact_head_evidence_and_invalidation`, `test_core_reset_v011.CoreResetTests.test_descendant_candidate_reuses_preserved_base_and_rejects_conflict`, `test_core_reset_v011.CoreResetTests.test_profile_drift_invalidates_candidate`, `test_core_reset_v011.CoreResetTests.test_validation_failure_never_creates_pack` |
  | HC-04 review terminality | `test_core_reset_v011.CoreResetTests.test_stream_events_distinguish_running_from_terminal`, `test_core_reset_v011.CoreResetTests.test_single_flight_reports_running` |
  | HC-05A consent | `test_task_guard.TaskGuardTests.test_dirty_owner_consent_yes_rearms_guard`, `test_task_guard.TaskGuardTests.test_dirty_owner_consent_no_clears_guard`, `test_task_guard.TaskGuardTests.test_changed_draft_does_not_reuse_stale_consent` |
  | HC-05B bounded continuation | `test_task_guard.TaskGuardTests.test_two_continuations_then_terminal_bounded_diagnostic`, `test_task_guard.TaskGuardTests.test_git_churn_never_resets_absolute_budget`, `test_task_guard.TaskGuardTests.test_real_progress_does_not_expand_absolute_budget` |

- The required literal `Hard oracle:` values are: HC-01 `Before/after state
  digest is identical`; HC-02 `Caller/foreign worktree diff is identical`;
  HC-03 `HEAD/tree/policy/profile digests match`; HC-04 `terminal=true and
  review_result_path != null`; and HC-05A/B `No bypass; continuation count <=
  contract`. The map is traceability evidence, not a replacement for the tests'
  behavioral oracle.
- Strengthen the existing HC-01 tests to compare `stable_digest(load_state())`
  immediately before and after negative and stale approval attempts. Strengthen
  the existing HC-04 stream test to require its completed result to contain a
  non-empty canonical `review_result_path` as well as `terminal=true`.
- Extend `scripts/validate_public_repo.py` with the smallest standard-library
  check: every required claim heading and fully-qualified `module.Class.method`
  ID exists in the suite discovered with `run_tests.py`'s discovery settings. A
  missing, moved, or non-discoverable ID exits non-zero. Its Markdown grammar
  is a `## HC-0x` heading, a literal `Hard oracle:` line, then one or more
  backticked fully-qualified test IDs; the required headings, exact oracle
  strings, and mapped IDs are fixed by the validator.
- Add `docs/evaluation-decisions.md`: a Markdown table with date, component,
  claim, exact version/HEAD, baseline, arm-manifest digest, cases, result,
  safety, cost/human delta, decision, next trigger, and privacy/retention
  status. Include one clearly synthetic M1-format record with no private data.
- The validator requires exactly this 13-column table header in order:
  `Date | Component | Claim | Exact version/HEAD | Baseline | Arm-manifest
  digest | Cases | Result | Safety | Cost/human delta | Decision | Next trigger
  | Privacy/retention`. It requires one exact synthetic row using the privacy
  allowlist below. It rejects a missing header/cell, altered allowlist value,
  or a record containing `/Users/`, `/private/`, `transcript`, `def `,
  `-----BEGIN PRIVATE KEY-----`, or `private-fixture:`.
- Add one focused standard-library validator test with an isolated documents
  root: it proves the valid map/log pass, then loops each required map heading,
  test ID, table header, and synthetic cell to prove its absence/alteration
  fails; it separately proves each prohibited-content class fails.
- Fix the demonstrated dependency defect: `dependency_status()` must reject a
  target whose current `accept` approval digest differs from the stored target
  definition digest. Add the focused regression where a target definition
  changes after acceptance, then a dependent candidate remains blocked.
- The synthetic row is not an M1-exit verdict. After EF-01 acceptance, the M2
  definition may record the acceptance evidence and receives a fresh independent
  definition review. Its implementation is gated by the existing DLS
  `accepted-in-base` dependency, not the Markdown record or copied receipt.

## Non-goals

- A new test/evaluation runner, generic evaluator, JSONL ledger, database,
  service, dashboard, or DLS state change.
- Live model calls, frozen semantic cases, iOS observations, ablations, or
  automation-trigger evidence.
- HC-06, HC-07, HC-08, and any release/production assessment.

## Current-system discovery

`plugins/dls/scripts/run_tests.py` discovers and executes the current
standard-library tests. `scripts/validate_public_repo.py` is the lightweight
public contract check. These existing commands are the only M1 automation.

The selected tests already cover stale and negative approval handling, owner
routing and dirty-owner stops, exact candidate/review evidence, terminal review
events, consent binding, and the absolute continuation limit. Therefore EF-01
adds no duplicate regression test unless implementation demonstrates a gap.

## Requirements and acceptance

- `REQ-001`: `evaluation-claim-map.md` includes HC-01, HC-02, HC-03, HC-04,
  HC-05A, and HC-05B. Each maps to an exact fully-qualified existing
  `module.Class.method` test ID and the
  appropriate hard oracle.
- `REQ-002`: `validate_public_repo.py` returns non-zero when a required map row
  or referenced test ID is absent from the suite discovered by `run_tests.py`.
  It also returns non-zero for each missing decision-log header, altered
  synthetic allowlist cell, and prohibited content class. The focused
  validator test and the full existing suite both pass.
- `REQ-003`: `evaluation-decisions.md` is a Markdown-only log with all required
  fields and a synthetic, privacy-safe record. It contains no local path, raw
  transcript, source, secret, or private fixture content.
- `REQ-004`: All EF-01 validation has zero model calls. Before M2
  implementation, DLS must store `dependency set EF-02 --on EF-01` with
  `requires=accepted-in-base` and the EF-01 target definition digest. At
  `candidate-ready`, the existing dependency check requires that digest to
  match, a current EF-01 `accept` approval whose digest equals the stored
  target definition digest and has `git_sha`, and that accepted SHA to be an
  ancestor of the M2 HEAD; it otherwise returns
  `wait-dependency` or `rebase-after-dependency`. This is the authoritative
  gate and is already covered by
  `test_core_reset_v011.CoreResetTests.test_dependency_requires_accepted_head_in_base`
  and the new definition-drift regression.
  A copied receipt tuple is audit evidence only. The synthetic row cannot
  authorize M2, release, or production.

<!-- dls:architecture:start -->
## Architecture and alternatives

The claim map remains Markdown because it is one human-auditable mapping, and
the existing public validator checks its required fully-qualified test IDs
against `run_tests.py`'s discovered suite. This is smaller than a new runner or
data format while retaining a non-zero failure for a missing claim. Existing
`run_tests.py` remains the execution authority.

Rejected alternatives:

- New L0 runner or JSONL ledger: duplicates existing execution and adds a
  second evidence surface before its trigger.
- LLM judge: hard claims have Git/state/test oracles and need no probabilistic
  judge.
<!-- dls:architecture:end -->

## Interfaces, state, and failure behavior

The public validator gains one documentation contract: missing M1 claim rows,
mapped test IDs, decision-log headers, safe synthetic values, or forbidden
private content are validation errors. `dependency_status()` gains one
integrity condition: an accepted definition digest must equal the dependent
change's recorded target digest. No public CLI, plugin manifest, hook, state
schema, or runtime API changes.

The decision log remains repository documentation. A malformed or incomplete
record is not a PASS and cannot claim M1 exit, release, or production. The
synthetic M1 row and any copied receipt are never authorization. The
authoritative M1-exit gate is DLS's existing accepted-in-base dependency check:
matching target definition digest, current accept SHA, and Git ancestry. The
later M2 definition is independently reviewed before implementation can begin.

## Security, privacy, data, and operations

The validator accepts the synthetic row only with this allowlist: an ISO date;
the public component `DLS L0`; claim `decision-log-format`; exact version/HEAD
`synthetic:format-check-v1`; baseline and arm digest `not-applicable-l0`; case
`synthetic-m1-format-01`; result `passed`; safety `not-evaluated`; cost/human
delta `not-measured`; decision `format-validated-not-m1-exit`; next trigger
`EF-01 accepted receipt`; and privacy status `no-private-artifact`.

`synthetic:format-check-v1` is an immutable public identifier, not source
content. A real later record may instead contain only `git:<40 lowercase hex>`
and immutable receipt digests. No record may contain a local repository path,
raw transcript, source content, secret, private fixture, or user data. For
later private artifacts, the log may say only `deleted` or
`retained-until:<date>`; the artifact is deleted after the decision or 30 days,
whichever is earlier.

No telemetry, network request, model call, or external storage is introduced.

## UI/UX contract

<!-- dls:design:start -->
Mode: bypass
Rationale: Documentation and validation-contract change; no user-interface
surface changes.
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

- `python3 plugins/dls/scripts/run_tests.py`
- `python3 scripts/validate_public_repo.py` in a clean public tree
- `python3 -m compileall -q plugins/dls/scripts plugins/dls/hooks scripts`

The checks must pass with zero model calls. Review and acceptance remain
separate gates; release and production are not evaluated.

## Risk rationale

Control level: standard. A wrong map could create a false safety claim or a
privacy leak in a durable decision record, so the definition and implementation
remain independently reviewed and digest-bound.
