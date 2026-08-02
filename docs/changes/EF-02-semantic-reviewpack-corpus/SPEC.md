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
5. expected verdict, expected lanes, maximum calls, and time/token ceilings;
6. privacy class `public-synthetic` and the owner of the hidden oracle.

Case text tells the reviewer only the task and ordinary ReviewPack context. The
hidden oracle, expected finding wording, fixture source, and result matcher
stay outside that reviewer prompt. A case has one expected reviewer verdict per
declared arm; an arm outcome is only `passed`, `product-failed`,
`component-failed`, `infrastructure-failed`, `invalid-case`, or
`budget-exhausted`.

### Record state, custody, and transition grammar

Every `docs/evaluation-m2-decisions.md` record has one immutable case ID, all
of that case's declared arms, and one `run_state`. Its fixed grammar permits
only these transitions:

| State | Required values | Forbidden values | Transition |
|---|---|---|---|
| `planned` | fixture/input/oracle/custody/manifest locks are `not-locked`; result, routing, attempts, metrics, and finding class are `not-run`; decision is `pending-live-sample` | terminal outcome or hard-oracle pass/fail | lock only |
| `locked-not-run` | all lock digests and custody digest are present; result fields remain `not-run` | terminal outcome or invented metrics | execute only |
| `completed` | terminal outcome and hard-oracle result for every declared arm, actual routing, every counted attempt, metrics or `unknown`, and finding class | `not-locked` or `not-run` result fields | none |

The document has exactly four records, one per SR ID, and one decision record.
While any case is non-completed, the decision is exactly
`pending-live-sample` with `not-applicable` rationale. Only after all four
records are completed may it become exactly one `keep`, `improve`, or `delete`
decision with its useful-evidence rationale. This is a transition contract,
not a fabricated result.

For reproducibility, the DLS maintainer creates one immutable private custody
bundle per case before `locked-not-run`. It contains the synthetic fixture
recipe, fixed Git metadata, and hidden-oracle implementation; its bundle digest
is recorded publicly. The maintainer retains the bundle for one year after the
M2 decision and gives an independent evaluator read-only access on request.
That evaluator recreates the disposable fixture privately and must reproduce
the recorded fixture SHA, tree/input/oracle digests, and custody digest before
running an arm. Raw live output is separate from custody and retains the
30-day limit below.

### Release-only procedure and records

`docs/evaluation-m2-runbook.md` must make live execution explicit and manual:

1. confirm EF-01 `accepted-in-base`, current plugin/version agreement, a fresh
   Codex task, and zero dirty source changes;
2. create the case's disposable synthetic Git fixture privately from its
   custody bundle, lock its SHA/digests, and validate its current manifest;
   validate a same-day reference manifest only for SR-03/SR-04;
3. run the current arm and, only for SR-03/SR-04, its allowed same-day
   reference. Immediately run the declared hard oracle, then record metrics as
   values or `unknown`;
4. stop on a hard-gate, privacy, budget, manifest, or infrastructure failure;
   only transport retries are permitted;
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
call, time, or token ceiling, it is not launched and the case becomes
`budget-exhausted`; without a permitted retry, a transport failure becomes
`infrastructure-failed`. A semantic retry is never allowed.

`docs/evaluation-m2-decisions.md` must contain exactly four case records and
one M2 decision in the preceding transition grammar. Every record has
case/arm/claim, fixture/input/oracle/custody and manifest digests, run state,
outcome, hard-oracle result, routing, every call attempt and successful-call
count, processed tokens/time or `unknown`, finding classification, and a
privacy-retention value. It must not contain a path, transcript, raw prompt,
session, source, secret, or a release/production claim.

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
infrastructure failure, or exhausted budget makes M2 incomplete and
`not-clear`.
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
  states and their transitions. The first implementation state is `planned`
  with complete contracts and no fabricated live result.
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
  and stops rather than reclassifying one as clear.
- `REQ-004`: No committed validation invokes `codex` or another live transport.
  A release-only run uses a fresh task and the pinned installed DLS plugin;
  reinstallation/hot reload during an arm invalidates that arm.
- `REQ-005`: M2 can exit only if every current arm and its declared reference
  behavior satisfy the hidden checks, current-arm dangerous blocker misses and
  safety violations equal zero, observed routing and all call attempts match
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

An incomplete field, mismatched lock/custody digest, unexpected lane/call
attempt count, failed hard oracle, privacy violation, invalid manifest,
infrastructure failure, or budget exhaustion records its typed outcome and ends
that case. It cannot create a clear case or M2 exit. A live model's wording is
untrusted evidence until the hidden oracle and matcher classify it.

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
