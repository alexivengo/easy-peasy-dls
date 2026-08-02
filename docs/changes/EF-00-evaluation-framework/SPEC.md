# Evaluation framework MVP definition — Specification

ID: `EF-00`

## Problem and outcome

Without a fixed claim, baseline, oracle, and cost boundary, an evaluation can
make DLS look successful after the fact. This definition makes the first
evaluation slice reproducible and bounded without creating a second DLS runtime
or evidence ledger.

## Scope

### Component claim registry

| Component ID | Component | Primary claim and observable oracle | Applies when | Decision owner |
|---|---|---|---|---|
| `decision-card-consent` | Definition/decision cards | HC-01: stale/negative consent leaves the state digest unchanged | definition or acceptance approval | DLS maintainer |
| `owner-routing-mutation` | Owner routing/worktree guard | HC-02: dirty, wrong, ambiguous, or foreign owner leaves caller and foreign tree digests unchanged | any implementation/remediation | DLS maintainer |
| `candidate-provenance` | Candidate/receipt provenance | HC-03: candidate/review HEAD, tree, policy, and profile digests equal the executed inputs | candidate-ready and review | DLS maintainer |
| `review-terminality` | Review runner/finalizer | HC-04: completion has `terminal=true` and non-null `review_result_path` | definition and code review | DLS maintainer |
| `guard-consent` | Completion guard | HC-05A: exact consent binding is retained only for the unchanged active draft | explicit implementation/remediation task | DLS maintainer |
| `guard-bound` | Completion guard | HC-05B: automatic continuation count is at most two per activation | explicit implementation/remediation task | DLS maintainer |
| `routing-selection` | Structured reviewer routing | selected lanes equal the routing policy for the control/risk | release-only semantic cases | DLS maintainer |
| `routing-early-stop` | Structured reviewer routing | actionable primary prevents a secondary call | release-only semantic cases | DLS maintainer |
| `profile-projection` | Platform profiles | resolved profile exposes only its declared capability set in the ReviewPack | profile-selected change | DLS maintainer |
| `named-validation` | Named validation commands | the configured named command completes with an evidence record | candidate/release gate | repository owner |

Internal helpers with no caller-visible behavior have no independent claim.
Bundled MCP is absent and is not an eval target.

A component ID is immutable for one eval-definition version. A causal
`component-off` arm is valid only when a frozen case declares that ID, a
predeclared `fixture-toggle` or isolated configuration switch, the on/off
configuration digests, and the exact clean-copy HEADs. The off arm may change
only that declared switch; both arms run the same non-target hard oracle. A
free-form patch or a self-declared manifest difference is not a component-off
arm. Until a frozen case supplies those fields, a component is L0-only and is
not eligible for causal M2 attribution.

### Baselines and hard gates

| Baseline | Allowed-difference rule | Use |
|---|---|---|
| Component-off | one skill, hook, prompt, route, profile, schema, or validation command | causal contribution |
| Previous release | arbitrary released changes are permitted but must be listed in the arm manifest | regression detection only; never causal attribution |
| Native Codex | DLS is absent; product oracle, permissions, task, model, effort, and toolchain are fixed | overall DLS overhead/value, not one-component attribution |
| Hard oracle | expected Git/state/artifact/test invariant | guarantees unique to DLS |

Every planned arm has a manifest containing case ID, exact HEAD/version, task
input digest, oracle version, model/effort, permissions, toolchain, enabled and
disabled components, and the reason for every allowed difference. The evaluator
first validates that manifest against the baseline rule, then executes the hard
oracle. Until automation is permitted, the manual checklist below performs
those same checks; an invalid manifest or hard-gate failure is rejected before
speed or cost comparison. The MVP hard gates are HC-01 through HC-05 from the
registry.
HC-05's authoritative limit is two automatic continuations per activation, as
defined by the current `plugins/dls/skills/dls-workflow/SKILL.md` contract and
enforced by the bundled task guard. Later HC-06 through HC-08 cover read-only
source, single owning model call, and honest lifecycle status.

The automation arm-count trigger is evaluated per case decision, not across a
release or corpus: it fires only when resolving one primary claim requires more
than two distinct arms. The planned four M2 cases each use at most a two-arm
comparison, so their combined arm count does not trigger automation.

Each case's expected reviewer verdict is `clear` or `not-clear`; it is separate
from the arm outcome. The only arm outcomes are:

| Outcome | Meaning | Aggregation and release treatment |
|---|---|---|
| `passed` | Expected verdict and hard oracle both pass | Eligible for the applicable quality/cost window |
| `product-failed` | Valid product oracle fails | Eligible negative result; release is `not-clear` |
| `component-failed` | Valid component oracle fails | Eligible negative result; release is `not-clear` |
| `infrastructure-failed` | Xcode, Simulator, signing, network, or model service fails without a demonstrated DLS cause | Excluded from quality/cost aggregation; release is incomplete and `not-clear` until a valid rerun |
| `invalid-case` | Manifest, case setup, or oracle is incomparable/invalid | Excluded; repair case/manifest; release is `not-clear` |
| `budget-exhausted` | Ceiling stops unscheduled or unfinished work | Excluded; release is incomplete and `not-clear` |

Missing telemetry is `unknown`/`null`. No outcome other than `passed` can be
summarized as a clear result.

### Decision and cost policy

Record only `safety_violations`, `manual_nudges_or_corrections`, `model_calls`,
`processed_tokens`, `wall_time_seconds`, and `review_cycles_to_clear_or_stop`.

Each case declares one expected reviewer verdict: `clear` or `not-clear`. An
applicable case exercises the component's primary claim and its declared oracle;
a case that does not do so is excluded with the reason recorded in its arm
manifest. A correct result has the expected verdict, a `passed` arm outcome,
and zero safety violations. A false block is an actual `not-clear` where the
expected verdict is `clear`.

The initial M2 four-case corpus is a per-case release gate: all four expected
labels and hard oracles must pass. It is not a 20-case quality sample. The
`19/20` correct-pass and at-most-`1/20` false-block thresholds apply only to the
rolling most-recent 20 applicable cases for one component and claim. Until that
window exists, the decision is `insufficient-data`, except that a demonstrated
unique high-impact escape may keep a quality component provisionally. Safety
always requires zero violations. Keep a cost component only after one saved
manual action or approximately 20% lower calls, tokens, or time with no quality
loss. Three release cycles or 30 applicable cases with no unique benefit starts
a delete/merge review. Rare safety guards use fault injection, not production
frequency.

Initial budgets: at most four live cases per week, six to eight release analysis
calls, 15 minutes of manual annotation for new/disputed findings, zero model
calls in commit/PR gates, and transport-only semantic retries. Budget exhaustion
is incomplete/not-clear, never clearance.

## Non-goals

- Persist raw prompts, transcripts, trajectories, proprietary source, secrets,
  or repository paths in canonical DLS state.
- Use a weighted quality score or LLM judge for a hard gate.
- Automate every commit or evaluate all components in an N-by-M matrix.
- Add a dependency or public interface for Harbor, a runner, or a ledger before
  its trigger.

## Current-system discovery

- `plugins/dls/tests/test_core_reset_v011.py` and
  `plugins/dls/tests/test_task_guard.py` already exercise state, ownership,
  review, and guard fault paths with fake Codex.
- `plugins/dls/scripts/run_tests.py` discovers the complete stdlib test suite;
  `scripts/validate_public_repo.py` validates the public package without extra
  dependencies.
- `status --details receipt` provides a deterministic projection. Review
  terminality requires `terminal=true` and a non-null `review_result_path`.
- The capability catalog is the source of the listed public components.

## Requirements and acceptance

- `REQ-001`: The component registry has exactly one primary observable claim per
  included component, names its applicability and owner, and excludes unused
  internals and bundled MCP.
- `REQ-002`: Each planned comparison names one baseline. Causal component-off
  arms differ only in the tested component; previous-release and Native Codex
  arms are explicitly non-causal and labeled regression or overall-overhead.
  Every arm rejects a hard-gate violation before any aggregate metric.
- `REQ-003`: Canonical DLS state stores only minimal evidence references,
  digests, model IDs, timestamps, and counters; raw live artifacts stay local
  and removable without invalidating a receipt.
- `REQ-004`: M1 is limited to mapping HC-01 through HC-05 onto the existing
  suite, filling only demonstrated coverage gaps, and creating one Markdown
  decision log. A runner/JSONL is forbidden until a documented trigger occurs.
- `REQ-005`: M0 preserves digest-bound human approval, one product owner,
  exact-HEAD evidence, read-only independent review, terminal review completion,
  bounded execution, and separate lifecycle states by changing no DLS runtime
  source, hook, state schema, or public CLI. M1 separately maps HC-01 through
  HC-05; HC-06 through HC-08 are explicitly post-MVP.

<!-- dls:architecture:start -->
## Architecture and alternatives

ADR-001 selects existing deterministic tests plus a Markdown decision log. It
keeps evaluation artifacts outside executable DLS state and promotes automation
only after a measured manual or scale trigger. Harbor is a P2 container-only
spike after M2, not an MVP dependency or iOS runtime substitute.
<!-- dls:architecture:end -->

## Interfaces, state, and failure behavior

M0 changes no public CLI, plugin manifest, hook, state schema, or runtime
behavior. Future decision-log records include date, component, claim, exact
version/HEAD, baseline, arm-manifest digest, cases, result, safety, cost/human
delta, decision, and next trigger. A malformed, incomparable, budget-exhausted,
or infrastructure-failed arm records an explicit incomplete outcome and cannot
become a clear result.

Before each M2 live call, the evaluator performs this deterministic manual
checklist in the decision log:

1. Record both complete arm manifests side by side and mark the baseline type.
2. For `component-off`, compare every manifest field and require exactly one
   enabled/disabled component difference. For `previous-release` and `Native
   Codex`, list every permitted difference and mark the run non-causal.
3. Run the listed hard oracle immediately after each arm, before recording any
   cost metric.
4. If step 1–3 fails, record `invalid-case`, `infrastructure-failed`, or
   `budget-exhausted` from the canonical taxonomy; record no aggregate metric
   and stop that comparison.

This checklist is the authorized M2 validation procedure until the automation
trigger permits a validator. The T04 runbook must include a synthetic invalid
manifest record that demonstrates step 4.

## Security, privacy, data, and operations

Public fixtures may contain only synthetic/minimal source. The DLS maintainer
owns private iOS artifacts and deletes each raw artifact after its decision is
recorded or after 30 days, whichever comes first. The owner verifies deletion
from the private store and records only `deleted` or `retained-until` plus the
expiry in the decision log; no path, source, transcript, or secret is recorded.
A receipt may refer only to a case ID and immutable content digest, so deleting
the private artifact never invalidates its canonical evidence. Third-party
harness telemetry is opt-in only. Commit/PR gates remain local, deterministic,
and make zero model calls.

## UI/UX contract

<!-- dls:design:start -->
Mode: bypass
Rationale: This definition and its planned test/decision-log artifacts have no
user-interface surface.
<!-- dls:design:end -->

## Validation intent

- `python3 plugins/dls/scripts/run_tests.py`
- `python3 scripts/validate_public_repo.py`
- `python3 -m compileall -q plugins/dls/scripts plugins/dls/hooks`
- Independent definition review for this exact definition digest.

## Risk rationale

Control level: standard. Incorrect governance could create false safety claims,
privacy leakage, or a parallel lifecycle source of truth; therefore definition
and architecture require digest-bound human approval.
