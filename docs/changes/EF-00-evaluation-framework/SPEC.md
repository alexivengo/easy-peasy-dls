# Evaluation framework MVP definition — Specification

ID: `EF-00`

## Problem and outcome

Without a fixed claim, baseline, oracle, and cost boundary, an evaluation can
make DLS look successful after the fact. This definition makes the first
evaluation slice reproducible and bounded without creating a second DLS runtime
or evidence ledger.

## Scope

### Component claim registry

| Component | Primary claim and observable oracle | Applies when | Decision owner |
|---|---|---|---|
| Definition/decision cards | HC-01: stale/negative consent leaves the state digest unchanged | definition or acceptance approval | DLS maintainer |
| Owner routing/worktree guard | HC-02: dirty, wrong, ambiguous, or foreign owner leaves caller and foreign tree digests unchanged | any implementation/remediation | DLS maintainer |
| Candidate/receipt provenance | HC-03: candidate/review HEAD, tree, policy, and profile digests equal the executed inputs | candidate-ready and review | DLS maintainer |
| Review runner/finalizer | HC-04: completion has `terminal=true` and non-null `review_result_path` | definition and code review | DLS maintainer |
| Completion guard | HC-05: exact consent binding is retained and continuation count is at most the contract limit | explicit implementation/remediation task | DLS maintainer |
| Structured reviewer routing | routing has the policy-selected lanes and no secondary call after actionable primary | release-only semantic cases | DLS maintainer |
| Platform profiles | resolved profile exposes only its declared capability set in the ReviewPack | profile-selected change | DLS maintainer |
| Named validation commands | the configured named command completes and its evidence records zero model calls | candidate/release gate | repository owner |

Internal helpers with no caller-visible behavior have no independent claim.
Bundled MCP is absent and is not an eval target.

### Baselines and hard gates

| Baseline | Allowed-difference rule | Use |
|---|---|---|
| Component-off | one skill, hook, prompt, route, profile, schema, or validation command | causal contribution |
| Previous release | arbitrary released changes are permitted but must be listed in the arm manifest | regression detection only; never causal attribution |
| Native Codex | DLS is absent; product oracle, permissions, task, model, effort, and toolchain are fixed | overall DLS overhead/value, not one-component attribution |
| Hard oracle | expected Git/state/artifact/test invariant | guarantees unique to DLS |

Every planned arm has a manifest containing case ID, exact HEAD/version, task
input digest, oracle version, model/effort, permissions, toolchain, enabled and
disabled components, and the reason for every allowed difference. The runner
first validates that manifest against the baseline rule, then executes the hard
oracle. An invalid manifest or hard-gate failure is rejected before speed or
cost comparison. The MVP hard gates are HC-01 through HC-05 from the registry.
Later HC-06 through HC-08 cover read-only source, single owning model call, and
honest lifecycle status.

Outcomes are `passed`, `product-failed`, `component-failed`,
`infrastructure-failed`, and `invalid-case`. Xcode, Simulator, signing,
network, and model-service failures are infrastructure failures unless the DLS
component is the demonstrated cause. Missing telemetry is `unknown`/`null`.

### Decision and cost policy

Record only `safety_violations`, `manual_nudges_or_corrections`, `model_calls`,
`processed_tokens`, `wall_time_seconds`, and `review_cycles_to_clear_or_stop`.

Safety requires zero violations. Normal corpus behavior requires at least
`19/20` correct passes and at most `1/20` false block. Keep a quality component
only after one uniquely prevented high-impact escape or two additional correct
results in 20 applicable cases. Keep a cost component only after one saved
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
- `REQ-002`: Each planned comparison names one baseline; arms differ only in the
  tested component and reject a hard-gate violation before any aggregate metric.
- `REQ-003`: Canonical DLS state stores only minimal evidence references,
  digests, model IDs, timestamps, and counters; raw live artifacts stay local
  and removable without invalidating a receipt.
- `REQ-004`: M1 is limited to mapping HC-01 through HC-05 onto the existing
  suite, filling only demonstrated coverage gaps, and creating one Markdown
  decision log. A runner/JSONL is forbidden until a documented trigger occurs.
- `REQ-005`: The framework preserves digest-bound human approval, one product
  owner, exact-HEAD evidence, read-only independent review, terminal review
  completion, bounded execution, and separate lifecycle states.

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
