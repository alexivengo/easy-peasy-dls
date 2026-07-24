---
name: dls-debug
description: Triage, reproduce, root-cause, fix, and prove a real bug, regression, failing test, crash, incident, or unexpected behavior through the smallest DLS path. Use only when the user explicitly invokes $dls-debug. Preserve evidence and human gates without turning a localized bug into an epic.
---

# DLS Debug

Use root-cause-first debugging inside DLS governance. Do not apply a speculative patch before establishing the failure mode unless an authorized hotfix must first contain active harm.

## Triage

1. Capture expected behavior, observed behavior, impact, environment, recency, and the narrowest reproduction evidence available.
2. Distinguish product defect, test defect, environment/tooling failure, configuration issue, external dependency, and documentation mismatch.
3. Choose the smallest route using [debug-paths.md](references/debug-paths.md).
4. Initialize or inspect DLS state with [cli.md](references/cli.md).

Ask one focused question at a time only when the answer changes reproduction, severity, scope, or acceptance.

## Reproduce and explain

Reproduce the reported failure on the relevant revision when feasible. Record drift if the current checkout no longer matches the report.

Rank plausible causes from evidence, inspect the smallest relevant execution path, and identify:

- the actual failure mechanism;
- why existing validation missed it;
- the smallest defensible fix point;
- edge cases and regression surface.

Do not confuse a passing nearby test with reproduction of the reported behavior.

## Fix

For micro or routine bugs, keep the whole loop in this task and use one compact `CHANGE.md` only when routine. Implement the smallest coherent fix and add the narrowest regression proof that fails for the original mechanism.

Escalate to standard when the fix changes material behavior, public contracts, multiple subsystems, or requires unresolved architectural trade-offs. Escalate to critical only for the consequences and triggers defined in [debug-paths.md](references/debug-paths.md).

UI bugs still need an exact accepted precedent or a scoped bypass. Security, privacy, concurrency, migration, data-loss, and availability tags may route to domain specialists, but DLS owns approvals and evidence.

## Verify and finish

Run focused regression proof, relevant surrounding tests, and any required runtime or environment gate. Record current evidence; do not claim fixed from code inspection.

Review the diff for collateral behavior. Use an independent acceptance-grade review only when the selected DLS path requires it or the residual uncertainty justifies it.

Report separately: reproduced, root cause established, fixed, regression-proven, broader validation, review status, and user acceptance.

## Boundaries

- No mandatory brainstorming, TDD ritual, worktree, subagent, or epic package.
- No production mutation or external remediation without explicit authorization.
- A hotfix containment exception does not become retrospective acceptance.
- Never infer approval from a generic acknowledgement.
