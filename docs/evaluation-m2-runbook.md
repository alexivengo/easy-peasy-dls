# M2 release runbook

## Preconditions

| Rule | Value |
|---|---|
| dependency | EF-01 accepted-in-base at d4b9e2f57c4061249d6ac346479aedd6149ed24e069f9b9c0552178b86d7b1c5 |
| plugin-version | dls 0.13.6+codex.20260802111333; reinstall or hot reload during an arm invalidates that arm |
| execution-profile | lock one plugin, agent, model, effort, and same-day run date in every record before manual-m2-arm |
| fresh-task | a new Codex task starts before the first arm; no restart during an arm |
| source-clean | the fixture and release-record worktree are clean at arm launch; after recording, validation, and commit they are clean before the next arm |
| manual-m2-arm | a release-authorized human invokes unchanged review-run --kind code in the declared disposable fixture; this M2 procedure does not restrict ordinary definition/code review |
| record-commit | write only the decision record in a dedicated release-record worktree after arm cleanup; validate and commit it before the next arm |

## Custody and locks

| Rule | Value |
|---|---|
| custody-bundle | one immutable private bundle per case with fixture recipe, fixed Git metadata, hidden oracle, per-arm execution receipts, SR-04 boundary receipt, and SR-04 execution proof |
| arm-receipt | every completed arm records an arm-receipt-v1 digest bound to its locks, profile, DLS result, hard oracle, matcher, counters, and actual fields; no raw artifact enters public record |
| source-blind-boundary | SR-04 repair accepts only prior review output and format error in a fresh empty temporary workspace with allowlist-empty environment and read-only sandbox; fixture, task source, hidden oracle, custody, network, and tool access are denied |
| lock-check | fixture, tree, input, oracle, custody, current/reference manifest, per-arm difference, and SR-04 repair-boundary locks match before a live arm |
| repair-proof | a completed SR-04.repair records a source-blind-v1 proof digest bound to that arm; the proof carries only digests, empty-temporary workspace, allowlist-empty environment, read-only sandbox, and zero denied reads |
| private-replay | an authorized evaluator and public validator fixtures use the canonical arm-receipt-v1/source-blind-v1 byte format to reproduce every lock, recompute every arm receipt and the SR-04 proof digest, and reject any mismatch before a clear M2 outcome |

## Arm order

| Rule | Value |
|---|---|
| SR-01 | SR-01.current |
| SR-02 | SR-02.current |
| SR-03 | SR-03.current then SR-03.primary-only on the same day |
| SR-04 | SR-04.repair then SR-04.fail-closed on the same day |

## Attempt accounting

| Rule | Value |
|---|---|
| attempt-syntax | primary=n;secondary=n;repair=n;transport-failed=n |
| successful-call-syntax | primary=n;secondary=n;repair=n |
| sample-budget | seven nominal calls; at most eight calls across SR-01 through SR-04 |
| transport-retry | one sample-wide retry before a semantic result and within its case and sample ceilings |

## Stop outcomes

| Rule | Value |
|---|---|
| hard-gate | a current safety violation or failed current hard oracle stops that case and makes M2 not-clear |
| invalid-case | only SR-04.fail-closed is the expected contrast invalid-case; every other invalid-case makes M2 not-clear |
| infrastructure-failed | missing cumulative meter, transport failure without the permitted retry, or unavailable lock evidence makes M2 not-clear |
| budget-exhausted | a call that would exceed call, time, or token ceiling is not launched and makes M2 not-clear |

## Record transition

| Rule | Value |
|---|---|
| planned | the planned profile uses not-locked lock placeholders except required not-applicable values; actual arm values and meters not-run; custody retained-for:365d-after-decision |
| locked-not-run | all locks match the case record; actual arm values not-run; custody retained-for:365d-after-decision |
| completed | terminal arm values and cumulative meters recorded; every executed arm requires its verified receipt and SR-04.repair its verified proof digest before clear; missing meter is infrastructure-failed |
| aborted | a stop writes decision_state=aborted and m2_outcome=not-clear; the executed case prefix is retained and all later case records stay unrun |
| decision | keep/improve/delete only for a clear M2 outcome with useful evidence; otherwise not-applicable |

## Retention

| Rule | Value |
|---|---|
| custody-retention | retain each private bundle through the verified date at least 365 days after the final M2 decision |
| raw-output-retention | keep raw private output no longer than 30 days or the final decision, whichever is earlier |
| public-record | public synthetic locks, typed outcomes, counters, and dates only; no path, prompt, transcript, source, session, or secret |
