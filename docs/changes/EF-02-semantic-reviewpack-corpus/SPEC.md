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

| ID | Claim and arms | Expected verdict and hard oracle | Live call bound |
|---|---|---|---:|
| `SR-01` | Clean-control/current | `clear`; no seeded defect and no invented evidence | 1 |
| `SR-02` | Seeded root-cause/current | `not-clear`; review finding leads to the hidden oracle passing after its fix | 1 |
| `SR-03` | Critical routing/current and primary-only component-off | `not-clear`; hidden secondary finding is useful only when primary is clean | 3 |
| `SR-04` | Malformed output/compact repair and fail-closed reference | unchanged semantic verdict; repair is source-blind and at most once | 2 |

Each case must lock, before its first live arm:

1. case ID and primary component claim;
2. synthetic fixture commit SHA and source-tree digest;
3. task-input digest and oracle version/digest;
4. current/reference arm-manifest digests and every permitted difference;
5. expected verdict, expected lanes, maximum calls, and time/token ceilings;
6. privacy class `public-synthetic` and the owner of the hidden oracle.

Case text tells the reviewer only the task and ordinary ReviewPack context. The
hidden oracle, expected finding wording, fixture source, and result matcher
stay outside that reviewer prompt. A case has one expected reviewer verdict;
an arm outcome is only `passed`, `product-failed`, `component-failed`,
`infrastructure-failed`, `invalid-case`, or `budget-exhausted`.

### Release-only procedure and records

`docs/evaluation-m2-runbook.md` must make live execution explicit and manual:

1. confirm EF-01 `accepted-in-base`, current plugin/version agreement, a fresh
   Codex task, and zero dirty source changes;
2. create the case's disposable synthetic Git fixture outside the repository,
   lock its SHA/digests, and validate the two arm manifests side by side;
3. run the current arm and its allowed reference on the same day, immediately
   run the declared hard oracle, then record metrics as values or `unknown`;
4. stop on a hard-gate, privacy, budget, manifest, or infrastructure failure;
   only transport retries are permitted;
5. delete raw local live artifacts after recording their digest-only outcome or
   after 30 days, whichever is earlier.

`docs/evaluation-m2-decisions.md` must contain exactly four case records and
one M2 decision. Every record has case/arm/claim, fixture/input/oracle and
manifest digests, outcome, hard-oracle result, routing and call count,
processed tokens/time or `unknown`, finding classification, and a
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
numbers. A dangerous miss, hard-gate violation, invalid case, infrastructure
failure, or exhausted budget makes M2 incomplete and `not-clear`.

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
  validated by the existing public validator and a focused stdlib test. Their
  first implementation state contains complete case/runbook contracts but no
  fabricated live result.
- `REQ-002`: Four fixture locks are created from clean synthetic Git fixtures;
  each live review starts only after all required fields and paired manifests
  are present. SR-01/SR-02 use one primary call, SR-03 uses a current critical
  two-lane arm plus a one-lane component-off reference, and SR-04 uses one
  primary plus at most one source-blind repair. The release sample therefore
  remains within six to eight calls.
- `REQ-003`: The runbook rejects a manifest that changes more than its declared
  component or lacks an applicable hard oracle before cost/quality comparison.
  It declares `invalid-case`, `infrastructure-failed`, or `budget-exhausted`
  and stops rather than reclassifying one as clear.
- `REQ-004`: No committed validation invokes `codex` or another live transport.
  A release-only run uses a fresh task and the pinned installed DLS plugin;
  reinstallation/hot reload during an arm invalidates that arm.
- `REQ-005`: M2 can exit only if SR-01…SR-04 have passed their hidden oracles,
  dangerous blocker misses and safety violations equal zero, observed routing
  and calls match each case contract, all live calls stay within budget, and
  the M2 record contains a useful-evidence decision. This remains evaluation
  evidence, not release or production approval.

<!-- dls:architecture:start -->
## Architecture and alternatives

The selected architecture is three static Markdown artifacts plus existing DLS
ReviewPack production and the existing stdlib validator. This keeps fixtures
disposable, locks auditable, and delivery state authoritative without a second
runtime.

Rejected alternatives:

- A generic fixture runner or JSONL ledger now: no automation trigger has been
  measured; it would duplicate the manual M2 procedure.
- Persisting fixture sources or raw reviewer output: it leaks more than the
  digest-only evidence needed to reproduce a decision.
- LLM judging or prose similarity: hidden executable oracles and bounded human
  adjudication are the authoritative matchers.
<!-- dls:architecture:end -->

## Interfaces, state, and failure behavior

No public DLS interface, state schema, hook, or runtime contract changes. The
new Markdown documents are documentation only and never executable input. The
public validator reads their fixed grammar; it does not create fixtures or
make network/model calls. Existing DLS receipts continue to own candidate,
review, acceptance, release, and production lifecycle facts.

An incomplete field, mismatched lock, unexpected lane/call count, failed hard
oracle, privacy violation, invalid manifest, infrastructure failure, or budget
exhaustion records its typed outcome and ends that case. It cannot create a
clear case or M2 exit. A live model's wording is untrusted evidence until the
hidden oracle and matcher classify it.

## Security, privacy, data, and operations

All public cases are synthetic. Committed M2 records contain only immutable
digests, model/plugin identifiers, dates, counters, typed outcomes, and a
retention state. Local raw output is optional private evidence, is never a
canonical DLS artifact, and is deleted after the decision or no later than 30
days. No document may contain a filesystem path, raw prompt/transcript,
repository source, session token, credential, or private fixture marker.

Live execution is release-only and outside ordinary CI. It runs at most four
cases and six to eight analysis/repair calls in a week; it stops before a fifth
case or ninth call. Only a transport retry is allowed; semantic retries change
the sample and require a separate later release event.

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
