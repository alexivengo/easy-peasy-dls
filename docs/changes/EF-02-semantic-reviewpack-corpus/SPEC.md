# Semantic ReviewPack corpus — Specification

ID: `EF-02`

## Problem and outcome

M1 proves deterministic lifecycle claims but cannot measure semantic review
noise, dangerous misses, critical secondary routing, or compact repair. M2
adds the smallest release-only evidence slice: four synthetic frozen cases,
manual bounded execution, and Markdown records. It does not automate the
procedure or grant a release decision.

## Scope

### Frozen-case contract

`docs/evaluation-m2-cases.md` must contain exactly these ordered case IDs:

| ID | Arms and reference rule | Expected verdict and hard oracle | Counted attempts |
|---|---|---|---:|
| `SR-01` | Current-only; reference fields are `not-applicable` | current `review-clear`; no seeded defect and no invented evidence | 1 / 2 |
| `SR-02` | Current-only; reference fields are `not-applicable` | current `not-clear`; its fix makes the hidden oracle pass | 1 / 2 |
| `SR-03` | Current critical plus primary-only component-off reference | current `not-clear`; primary-only `review-clear` is the expected dangerous miss; secondary finding is useful only when primary is clean | 3 / 4 |
| `SR-04` | Compact-repair current plus deterministic fail-closed reference | repair `review-clear`; reference is `invalid-case` with no clear verdict; source-blind repair preserves the clean-control oracle | 2 / 3 |

The first number is the nominal attempt count; the second is that case's
ceiling if the sample's single transport-retry slot is used. The four nominal
arms use seven attempts. The whole sample permits at most one additional
transport attempt and therefore never exceeds eight.

Each case must lock, before its first live arm:

1. case ID and primary component claim;
2. synthetic fixture commit SHA and source-tree digest;
3. task-input digest and oracle version/digest;
4. current arm-manifest digest and every permitted difference; a reference
   manifest digest only for SR-03/SR-04, otherwise the literal
   `not-applicable`;
5. expected verdict, expected lanes, a literal per-arm maximum-call contract,
   and time/token ceilings;
6. privacy class `public-synthetic`, the hidden-oracle owner, and (for SR-04)
   the digest of its source-blind repair-access proof.

Case text tells the reviewer only the task and ordinary ReviewPack context. The
hidden oracle, expected finding wording, fixture source, and result matcher
stay outside that reviewer prompt. A case has one expected reviewer verdict per
declared arm; an arm outcome is only `passed`, `product-failed`,
`component-failed`, `infrastructure-failed`, `invalid-case`, or
`budget-exhausted`.

### Normative Markdown field schema

The public validator treats the following headings, field names, table headers,
and order as literals. It rejects an unknown or missing field, an extra arm, a
reordered arm, a duplicate field, or a value outside the named enum.

`docs/evaluation-m2-cases.md` has, in order, `# M2 frozen cases`,
`## Case registry`, and `## SR-01` through `## SR-04`. Its registry header is
exactly `Case | Current arm | Reference arm | Nominal attempts | Retry ceiling`.
Its ordered rows are:

| Case | Current arm | Reference arm | Nominal attempts | Retry ceiling |
|---|---|---|---:|---:|
| `SR-01` | `SR-01.current` | `not-applicable` | 1 | 2 |
| `SR-02` | `SR-02.current` | `not-applicable` | 1 | 2 |
| `SR-03` | `SR-03.current` | `SR-03.primary-only` | 3 | 4 |
| `SR-04` | `SR-04.repair` | `SR-04.fail-closed` | 2 | 3 |

Each case section has a `Case fields` table with the exact header `Field |
Value` and this field order:

`case_id`, `claim`, `fixture_sha`, `tree_digest`, `task_input_digest`,
`oracle_version`, `oracle_digest`, `oracle_owner`, `custody_digest`,
`repair_access_digest`, `current_manifest_digest`, `reference_manifest_digest`,
`permitted_manifest_difference`, `time_ceiling_seconds`, `token_ceiling`,
`privacy`, `custody_retention`.

Every digest is lowercase `sha256:<64 hex>` when locked and `not-locked` only
in `planned`; `fixture_sha` is lowercase `git:<40 hex>` when locked.
`reference_manifest_digest` is `not-applicable` only for SR-01/SR-02.
`oracle_owner` is exactly `dls-maintainer`; `repair_access_digest` is
`not-applicable` except for SR-04, where it is a locked digest of the private
source-blind access proof. `privacy` is exactly `public-synthetic`;
`time_ceiling_seconds` and `token_ceiling` are positive base-10 integers.
`custody_retention` is exactly `retained-for:365d-after-decision` before the
final M2 decision; the decision-record transition later replaces it with the
verified `retained-until:YYYY-MM-DD` date. `permitted_manifest_difference` is
exactly one of:

| Arm | Value |
|---|---|
| `SR-01.current` | `none` |
| `SR-02.current` | `none` |
| `SR-03.current` | `none` |
| `SR-03.primary-only` | `secondary-lane=disabled` |
| `SR-04.repair` | `repair-mode=compact` |
| `SR-04.fail-closed` | `repair-mode=fail-closed` |

`docs/evaluation-m2-decisions.md` has `# M2 release records`, `## SR-01`
through `## SR-04`, then `## M2 decision`, in that order. Each case has a
`Record fields` table with header `Field | Value` and this exact field order:

`case_id`, `run_state`, `fixture_sha`, `tree_digest`, `task_input_digest`,
`oracle_version`, `oracle_digest`, `custody_digest`, `current_manifest_digest`,
`reference_manifest_digest`, `processed_tokens`, `wall_time_seconds`,
`custody_retention`, `privacy_retention`.

It follows with an `Arm records` table whose exact header is `Arm | Expected
verdict | Expected lanes | Call contract | Repair access | Actual verdict |
Outcome | Hard oracle | Safety violations | Lanes | Attempts | Successful calls |
Finding class`. Its rows use the arm order in the registry: the current arm
first, then the reference arm where one exists.
`docs/evaluation-m2-cases.md` uses that same `Arm records` table with expected
columns populated and all actual columns set to `not-run`.

`Expected verdict` and `Actual verdict` are one of `review-clear`,
`not-clear`, `not-applicable`, or `not-run`. `Expected lanes` and `Lanes` are
one of `primary`, `primary,secondary`, `none`, or `not-run`. `Outcome` is one
of `passed`, `product-failed`, `component-failed`, `infrastructure-failed`,
`invalid-case`, `budget-exhausted`, or `not-run`. `Hard oracle` is `passed`,
`failed`, or `not-run`. `Finding class` is `useful`, `noisy`,
`dangerous-miss`, `uncertain`, `no-finding`, `not-applicable`, or `not-run`.
SR-01.current and SR-04.repair use `no-finding` on a clear result;
SR-04.fail-closed uses `not-applicable`; no other literal stands for an absent
finding. `Call contract` is exactly
`primary=<n>;secondary=<n>;repair=<n>;transport-retry<=<n>`, with each `<n>` a
non-negative base-10 integer. Its six fixed values are:

| Arm | Call contract | Repair access |
|---|---|---|
| `SR-01.current` | `primary=1;secondary=0;repair=0;transport-retry<=1` | `not-applicable` |
| `SR-02.current` | `primary=1;secondary=0;repair=0;transport-retry<=1` | `not-applicable` |
| `SR-03.current` | `primary=1;secondary=1;repair=0;transport-retry<=1` | `not-applicable` |
| `SR-03.primary-only` | `primary=1;secondary=0;repair=0;transport-retry<=1` | `not-applicable` |
| `SR-04.repair` | `primary=1;secondary=0;repair=1;transport-retry<=1` | `source-blind:review-output+format-error` |
| `SR-04.fail-closed` | `primary=0;secondary=0;repair=0;transport-retry<=0` | `not-applicable` |

`Repair access` is checked literally. For SR-04.repair it means the compact
repair receives only the prior review output and its format error: no fixture
source, task source, hidden oracle, or private custody content. Its private
access proof must have zero such reads and hash to the case's
`repair_access_digest`. Before execution `Safety violations` is `not-run`; once
completed it is a non-negative base-10 integer for every current arm and
`not-applicable` for every reference arm. It counts a hidden-oracle confirmed
dangerous defect that reached `review-clear`, plus a failed current-arm safety
hard oracle. A nonzero value is a hard-gate failure.

`Attempts` is exactly `primary=<n>;secondary=<n>;repair=<n>;transport-failed=<n>`
and `Successful calls` is exactly `primary=<n>;secondary=<n>;repair=<n>`, with
each `<n>` a non-negative base-10 integer. Before execution both are `not-run`.
For `decision_state=completed`, the validator sums every arm's attempt counters
and requires exactly seven for a no-retry sample or eight with exactly one
transport-failed attempt; the matching per-case ceiling and all successful-call
counters must agree. For `decision_state=aborted`, a partial total from zero
through eight is permitted, with at most one transport-failed attempt; every
counter still remains within its arm and sample ceilings. A counter never
exceeds its arm's `Call contract`; a passed arm's successful-call counter equals
its contracted primary/secondary/repair counts.

`docs/evaluation-m2-runbook.md` has, in order, `# M2 release runbook`,
`## Preconditions`, `## Custody and locks`, `## Arm order`, `## Attempt
accounting`, `## Stop outcomes`, `## Record transition`, and `## Retention`.
Every section has exactly one `Rule | Value` table. The exact ordered rules are
`dependency`, `plugin-version`, `fresh-task`, `source-clean`;
`custody-bundle`, `lock-check`, `private-replay`; `SR-01`, `SR-02`, `SR-03`,
`SR-04`; `attempt-syntax`, `successful-call-syntax`, `sample-budget`,
`transport-retry`; `hard-gate`, `invalid-case`, `infrastructure-failed`,
`budget-exhausted`; `planned`, `locked-not-run`, `completed`, `aborted`,
`decision`; and
`custody-retention`, `raw-output-retention`, `public-record`. Their values are
the following exact literals; the validator asserts every heading, table, rule,
and value rather than executing a command from the document.

| Section | Rule | Value |
|---|---|---|
| Preconditions | `dependency` | `EF-01 accepted-in-base at d4b9e2f57c4061249d6ac346479aedd6149ed24e069f9b9c0552178b86d7b1c5` |
| Preconditions | `plugin-version` | `dls 0.13.6+codex.20260802111333; reinstall or hot reload during an arm invalidates that arm` |
| Preconditions | `fresh-task` | `a new Codex task starts before the first arm; no restart during an arm` |
| Preconditions | `source-clean` | `the fixture and DLS source are clean before and after each arm` |
| Custody and locks | `custody-bundle` | `one immutable private bundle per case with fixture recipe, fixed Git metadata, hidden oracle, and SR-04 access proof` |
| Custody and locks | `lock-check` | `fixture, tree, input, oracle, custody, manifest, and SR-04 repair-access digests match before a live arm` |
| Custody and locks | `private-replay` | `an authorized evaluator receives read-only bundle access and reproduces every recorded lock before an arm` |
| Arm order | `SR-01` | `SR-01.current` |
| Arm order | `SR-02` | `SR-02.current` |
| Arm order | `SR-03` | `SR-03.current then SR-03.primary-only on the same day` |
| Arm order | `SR-04` | `SR-04.repair then SR-04.fail-closed on the same day` |
| Attempt accounting | `attempt-syntax` | `primary=n;secondary=n;repair=n;transport-failed=n` |
| Attempt accounting | `successful-call-syntax` | `primary=n;secondary=n;repair=n` |
| Attempt accounting | `sample-budget` | `seven nominal calls; at most eight calls across SR-01 through SR-04` |
| Attempt accounting | `transport-retry` | `one sample-wide retry before a semantic result and within its case and sample ceilings` |
| Stop outcomes | `hard-gate` | `a current safety violation or failed current hard oracle stops that case and makes M2 not-clear` |
| Stop outcomes | `invalid-case` | `only SR-04.fail-closed is the expected contrast invalid-case; every other invalid-case makes M2 not-clear` |
| Stop outcomes | `infrastructure-failed` | `missing cumulative meter, transport failure without the permitted retry, or unavailable lock evidence makes M2 not-clear` |
| Stop outcomes | `budget-exhausted` | `a call that would exceed call, time, or token ceiling is not launched and makes M2 not-clear` |
| Record transition | `planned` | `all locks not-locked; actual arm values not-run; custody retained-for:365d-after-decision` |
| Record transition | `locked-not-run` | `all locks match the case record; actual arm values not-run; custody retained-for:365d-after-decision` |
| Record transition | `completed` | `terminal arm values and cumulative meters recorded; missing meter is infrastructure-failed` |
| Record transition | `aborted` | `a stop writes decision_state=aborted and m2_outcome=not-clear; the executed case prefix is retained and all later case records stay unrun` |
| Record transition | `decision` | `keep/improve/delete only for a clear M2 outcome with useful evidence; otherwise not-applicable` |
| Retention | `custody-retention` | `retain each private bundle through the verified date at least 365 days after the final M2 decision` |
| Retention | `raw-output-retention` | `keep raw private output no longer than 30 days or the final decision, whichever is earlier` |
| Retention | `public-record` | `public synthetic locks, typed outcomes, counters, and dates only; no path, prompt, transcript, source, session, or secret` |

The focused test and public validator expose one assertion per schema rule:

| Assertion | Schema coverage |
|---|---|
| `m2-document-order` | all three heading sequences, case order, and arm order |
| `m2-field-shape` | every table header, field order, duplicate/unknown field rejection, oracle owner, repair-access lock, call contract, safety count, and decision date |
| `m2-enums` | digest/SHA syntax, literals, lanes, outcomes, finding classes, metrics, and retention |
| `m2-state-transition` | planned, locked-not-run, completed, aborted, retention-date, and pre-live no-result values |
| `m2-attempt-budget` | arm contracts/counters, seven/eight completed samples, and bounded partial aborted samples |
| `m2-metering` | authoritative cumulative meters; unknown meter makes the sample non-clear |
| `m2-overall-outcome` | current safety/hard-oracle predicate and the two allowed contrast references |
| `m2-decision-evidence` | clear M2 outcome plus completed useful arm token required for keep/improve/delete |
| `m2-privacy` | prohibited raw artifact markers and non-executable document boundary |

### Record state, custody, and transition grammar

Every `docs/evaluation-m2-decisions.md` record has one immutable case ID, all
of that case's declared arms, and one `run_state`. Its fixed grammar permits
only these transitions and the field values from the normative schema:

| State | Required values | Forbidden values | Transition |
|---|---|---|---|
| `planned` | every lock field is `not-locked`; custody is `retained-for:365d-after-decision`; every actual arm column is `not-run`; decision is `pending-live-sample` | a locked digest, terminal result, or metrics | lock only |
| `locked-not-run` | every required lock/custody value matches its case record; custody is `retained-for:365d-after-decision`; every actual arm column remains `not-run` | terminal outcome, counter, or invented metric | execute only |
| `completed` | terminal values for every executed arm, with hard oracle, safety count, lanes, attempt counters, successful-call counters, cumulative metrics, and finding class; after a stop arm, later declared arms may remain wholly `not-run` | an unrun arm before a terminal stop arm or an invented metric | none |

The document has exactly four records, one per SR ID, and one decision record.
`## M2 decision` has a `Decision fields` table with exact header `Field | Value`
and row order `decision_state`, `decision_date`, `m2_outcome`, `decision`,
`evidence`. While no stop has occurred and any case is non-completed, its values
are `pending-live-sample`, `not-run`, `not-run`, `not-applicable`, and
`not-applicable`.

A hard-gate, privacy, manifest, budget, or infrastructure stop makes the
decision terminal `aborted`, an ISO `YYYY-MM-DD` decision date, `not-clear`,
`not-applicable`, and `not-applicable`. In that state, one or more completed
records form a contiguous prefix in SR-01…SR-04 order; the last completed record
may have a terminal stop arm followed only by wholly `not-run` arms; all
following records remain `planned` or `locked-not-run` with no attempts. The
stop arm must record
one of `product-failed`, `component-failed`, `infrastructure-failed`,
`invalid-case`, or `budget-exhausted`, or a failed hard oracle/nonzero safety
count. No arm after the stop arm may have a call counter. This is the only
terminal state that permits an unrun case or a partial attempt total.

Only after all four records are completed may the decision values become
`completed`, an ISO `YYYY-MM-DD` decision date, and `clear` or `not-clear`. At
either terminal transition every completed case's `custody_retention` becomes
`retained-until:YYYY-MM-DD`, a date at least 365 calendar days after
`decision_date` (the validator compares calendar dates, including leap years).

`m2_outcome` is `clear` only for `decision_state=completed` when every current
arm has outcome `passed`, hard oracle `passed`, zero safety violations, its
exact expected verdict/lanes, and
contract-bounded counters; both cumulative case meters are known non-negative
integers within the case ceilings; SR-03.primary-only has its expected
`review-clear`/`dangerous-miss` contrast; and SR-04.fail-closed has its expected
`not-applicable`/`invalid-case` contrast. Any other product/component failure,
failed hard oracle, current dangerous miss, nonzero safety count, missing meter,
infrastructure failure, budget exhaustion, invalid case, routing/call mismatch,
or reference result makes it `not-clear`; an `aborted` decision is always
`not-clear`. `keep`, `improve`, or `delete` is
permitted only for `clear`, with one or more semicolon-separated
`SR-##.<arm>:useful` tokens. Each token must name a completed arm whose
`Finding class` is exactly `useful`; `no-finding`, `noisy`, `dangerous-miss`,
`uncertain`, and `not-applicable` cannot support a decision. For `not-clear`,
both `decision` and `evidence` are exactly `not-applicable`. This is a
transition contract, not a fabricated result.

For reproducibility, the `dls-maintainer` creates one immutable private custody
bundle per case before `locked-not-run`. It contains the synthetic fixture
recipe, fixed Git metadata, and hidden-oracle implementation; SR-04 also
contains its source-blind access proof. Its bundle digest (and SR-04
repair-access digest) is recorded publicly. The maintainer retains the bundle
through the verified retention date and gives an independent evaluator read-only
access on request. That evaluator recreates the disposable fixture privately,
verifies the owner and all recorded locks, and must reproduce the fixture SHA,
tree/input/oracle/custody digests before running an arm. For SR-04 it must also
verify that the compact repair proof has zero fixture/source/hidden-oracle
reads. Raw live output is separate from custody and retains the 30-day limit
below.

### Release-only procedure and records

`docs/evaluation-m2-runbook.md` must make live execution explicit and manual:

1. confirm EF-01 `accepted-in-base`, current plugin/version agreement, a fresh
   Codex task, and zero dirty source changes;
2. create the case's disposable synthetic Git fixture privately from its
   custody bundle, lock its SHA/digests, and validate its current manifest;
   validate a same-day reference manifest only for SR-03/SR-04;
3. run the current arm and, only for SR-03/SR-04, its allowed same-day
   reference. Immediately run the declared hard oracle, then record the
   authoritative cumulative time/token meters for every launched attempt; an
   unavailable meter records `unknown`, makes the affected arm
   `infrastructure-failed`, and prevents a clear M2 outcome;
4. stop on a hard-gate, privacy, budget, manifest, or infrastructure failure;
   record its terminal `aborted`/`not-clear` decision, preserve its executed
   prefix and leave all later cases unrun; only transport retries are permitted;
5. delete raw local live artifacts after recording their digest-only outcome or
   after 30 days, whichever is earlier.

Every launched model invocation is a `call_attempt`, including a transport
failure and a repair call. It consumes that case's attempt ceiling, the sample
ceiling, elapsed time, and processed-token ceiling even when its token value is
`unknown`. A `successful_call` is only a completed invocation and is recorded
separately. Routing is evaluated from the completed terminal arm; failed
transport attempts record lane `none` and never satisfy a routing obligation.

At most one transport retry is allowed across the four-case sample. It is
allowed only before a semantic result and only when the next attempt fits both
the case's retry ceiling and the sample ceiling of eight. If it would exceed a
call, time, or token ceiling, it is not launched, that arm records
`budget-exhausted` with its other actual fields `not-run`, and the sample
becomes `aborted`; without a permitted retry, a transport failure becomes
`infrastructure-failed` and the sample becomes `aborted`. A semantic retry is
never allowed.

`docs/evaluation-m2-decisions.md` must contain exactly four case records and
one M2 decision in the preceding transition grammar. Case/arm fields, values,
and ordering are exactly the normative schema. Each completed case additionally
has `processed_tokens` and `wall_time_seconds` in its `Record fields` table.
Each is an authoritative cumulative non-negative base-10 integer across every
launched attempt or `unknown`; `unknown` is permitted only with an
`infrastructure-failed` arm and requires overall M2 `not-clear`. Planned and
locked-not-run use `not-run`. `privacy_retention` is `not-applicable` before
completion and then `deleted` or `retained-until:YYYY-MM-DD`. It must not contain a path,
transcript, raw prompt, session, source, secret, or a release/production claim.

### Finding matcher

- `useful`: describes a violated behavior and its prescribed fix makes the
  hidden oracle pass.
- `noisy`: is a blocker but the hidden oracle and bounded manual evidence do
  not confirm it.
- `dangerous-miss`: a seeded high-impact root cause is absent before a clear
  verdict.
- `uncertain`: bounded manual adjudication needs no more than 15 minutes.

The matcher has no second LLM judge and never scores exact prose or line
numbers. A dangerous miss, hard-gate violation, unintended invalid case,
infrastructure failure, unknown meter, or exhausted budget makes M2 incomplete
and `not-clear`.
The declared SR-03 primary-only reference is the sole exception: its expected
dangerous miss is comparison evidence for routing value, is recorded as such,
and never clears a current arm. Any dangerous miss in a current arm or an
undeclared reference remains M2-incomplete.
The declared SR-04 fail-closed reference is the only expected
`invalid-case`: it proves that malformed output cannot clear without repair.
It is comparison evidence, not a current-arm outcome; any other `invalid-case`
remains M2-incomplete.

## Non-goals

- New runtime schema, public command, runner, JSONL, database, dashboard,
  service, third-party dependency, or Harbor integration.
- More than the four fixed cases, normal CI/commit/PR live calls, iOS pilots,
  a general component ablation matrix, or release/production automation.
- A mutable update to the closed `docs/evaluation-decisions.md` M1 seed.

## Current-system discovery

- EF-01 is accepted at `9971d1843a2e545216d125f1dee60d6f0b19c83f` with
  definition digest
  `d4b9e2f57c4061249d6ac346479aedd6149ed24e069f9b9c0552178b86d7b1c5`.
  It is the only M2 implementation dependency.
- `plugins/dls/tests/test_core_reset_v011.py` already supplies deterministic
  fake-Codex fixtures for clean, primary/secondary routing, and repair paths.
  They are discovery references, not live semantic evidence.
- `python3 plugins/dls/scripts/run_tests.py`,
  `python3 scripts/validate_public_repo.py`, and compileall are deterministic
  and must stay model-free. `review-run` is release-only live execution.
- DLS accepts only repository-derived ReviewPacks; no public CLI accepts a
  caller-supplied pack path. The runbook therefore creates a disposable
  fixture that produces its own pack.

## Requirements and acceptance

- `REQ-001`: The three public M2 Markdown documents have an exact fixed grammar
  validated by the existing public validator and a focused stdlib test. It
  accepts only the defined `planned`, `locked-not-run`, and `completed` record
  states, terminal aborted path, cumulative-meter/retention rules, and their
  transitions. The first implementation state is `planned` with complete
  contracts and no fabricated live result.
- `REQ-002`: Four fixture locks are created from clean synthetic Git fixtures
  and matching private custody bundles. Each live review starts only after all
  required fields are `locked-not-run`. SR-01/SR-02 are current-only; SR-03
  uses a current critical two-lane arm plus a one-lane component-off reference;
  SR-04 uses one primary plus at most one source-blind repair and a
  zero-live-call fail-closed reference. The nominal sample has seven attempts;
  one counted transport retry may raise it only to eight.
- `REQ-003`: The runbook rejects a manifest that changes more than its declared
  component or lacks an applicable hard oracle before cost/quality comparison.
  It declares `invalid-case`, `infrastructure-failed`, or `budget-exhausted`
  and stops rather than reclassifying one as clear. It records a terminal
  `aborted`/`not-clear` outcome with partial attempts when needed. It uses exact
  rule values, arm order, plugin invalidation, call contracts, and a
  source-blind SR-04 repair proof.
- `REQ-004`: No committed validation invokes `codex` or another live transport.
  A release-only run uses a fresh task and the pinned installed DLS plugin;
  reinstallation/hot reload during an arm invalidates that arm.
- `REQ-005`: M2 can exit only if the validator's explicit clear-outcome
  predicate passes: every current arm and declared contrast reference satisfy
  the hidden checks, current-arm dangerous blocker misses and safety violations
  equal zero, meters are available, observed routing and all call attempts match
  each case contract, no retry exceeds a ceiling, and the completed M2 record
  contains a useful-evidence decision. The declared SR-03 reference miss and
  SR-04 fail-closed reference are comparison evidence only. This remains
  evaluation evidence, not release or production approval.

<!-- dls:architecture:start -->
## Architecture and alternatives

The selected architecture is three static Markdown artifacts plus existing DLS
ReviewPack production and the existing stdlib validator. This keeps fixtures
disposable, locks auditable, and delivery state authoritative without a second
runtime.

Rejected alternatives:

- A generic fixture runner or JSONL ledger now: no automation trigger has been
  measured; it would duplicate the manual M2 procedure.
- Persisting fixture sources or raw reviewer output in the repository/DLS:
  rejected. A private immutable custody bundle permits authorised replay while
  public records retain only its digest.
- LLM judging or prose similarity: hidden executable oracles and bounded human
  adjudication are the authoritative matchers.
<!-- dls:architecture:end -->

## Interfaces, state, and failure behavior

No public DLS interface, state schema, hook, or runtime contract changes. The
new Markdown documents are documentation only and never executable input. The
public validator reads their fixed grammar; it does not create fixtures or
make network/model calls. Existing DLS receipts continue to own candidate,
review, acceptance, release, and production lifecycle facts.

An incomplete field, mismatched lock/custody or repair-access digest, unexpected
lane/call contract, failed hard oracle, nonzero current safety count, unknown
meter, privacy violation, invalid manifest, infrastructure failure, or budget
exhaustion records its typed outcome, ends that case, and terminally aborts the
sample with its remaining cases unrun. It cannot create a clear case or M2 exit.
A live model's wording is untrusted evidence until the hidden oracle and matcher
classify it.

## Security, privacy, data, and operations

All public cases are synthetic. Committed M2 records contain only immutable
digests, model/plugin identifiers, dates, counters, typed outcomes, and a
retention state. Private custody bundles hold the recreation recipe and hidden
oracle for one year after the M2 decision; they are never DLS artifacts or
committed source. Local raw output is optional private evidence and is deleted
after the decision or no later than 30 days. No document may contain a
filesystem path, raw prompt/transcript, repository source, session token,
credential, or private fixture marker.

Live execution is release-only and outside ordinary CI. It runs at most four
cases and seven nominal analysis/repair attempts in a week; one counted
transport retry may raise the sample only to eight. It stops before a fifth
case or ninth attempt. Semantic retries change the sample and require a
separate later release event.

## UI/UX contract

<!-- dls:design:start -->
Mode: bypass
Rationale: M2 adds release documentation and synthetic evaluation evidence,
not a user-interface surface.
<!-- dls:design:end -->

## Validation intent

- `python3 plugins/dls/scripts/run_tests.py`
- `python3 scripts/validate_public_repo.py`
- `python3 -m compileall -q plugins/dls/scripts plugins/dls/hooks scripts`
- focused grammar/privacy regressions for M2 documents
- independent definition review for this exact digest
- release-only manual SR-01…SR-04 exercise only after definition acceptance,
  EF-01 accepted-in-base, and all locks are present

## Risk rationale

Control level: standard. M2 introduces bounded live analysis and decision
records. The definition locks scope, privacy, budgets, expected routing, hidden
oracles, typed incomplete outcomes, and human acceptance before implementation.
