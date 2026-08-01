---
name: dls-workflow
description: Run Easy Peasy DLS for a feature, fix, specification, implementation, exact-HEAD review, remediation, or acceptance. Activate when the user names DLS, the repository contains .dls state, or a supplied DLS change ID resolves through Git worktree ownership. Do not intercept generic work without a DLS signal.
---

# Easy Peasy DLS

DLS owns delivery state and mechanical proof. The user owns definition,
architecture/design decisions, and final acceptance.

Resolve the plugin CLI only from this installed skill: the plugin root is two
directories above this file. Require `.codex-plugin/plugin.json` and
`scripts/dls.py --version` to agree. Never probe `PATH` for `dls`, scan sibling
repositories, or fall back to a source/R&D checkout.

Read [cli.md](references/cli.md) before invoking the CLI and [gates.md](references/gates.md)
when asking for a human decision.

## Definition

- Use `CHANGE.md` for routine work. Standard/critical use the smallest useful
  EPIC/SPEC/TICKETS package; ADR is optional unless the decision deserves its
  own durable record.
- Architecture and design remain separate decisions derived from committed
  SPEC/ADR or an exact design source/bypass.
- Standard/critical definition approval requires a current independent
  `review-run CHANGE_ID --kind definition --stream`.
- Ask one approval question containing every pending decision and short digest.
  Record the atomic bundle only after the user explicitly confirms each item.
- A dependency only blocks implementation and always means
  `OTHER_CHANGE accepted-in-base`. It never blocks earlier definition work.

## Implementation and remediation

- Use one writer per change. For isolated standard/critical work, invoke
  `worktree prepare`; Git worktree identity is authoritative.
- Read `status CHANGE_ID` once. Follow its single `next_action`; do not inspect
  state internals or reconstruct history.
- Implement the accepted scope, run focused tests while coding, commit the
  candidate, then invoke only `candidate-ready`.
- If DLS already has a candidate lineage, do not supply or replace its review
  base. Invoke `candidate-ready` without `--base`; DLS reuses the preserved base.
- On remediation, read current findings via `status --details findings`.
  Declare each as `--address` or `--note`; never set `verified`.
- `candidate-ready` runs repository-owned required commands, records exact-HEAD
  evidence, and prepares the current ReviewPack. Stop at `open-review-task`.
- Never run code review from a standard/critical implementation task.

## Independent review

In a fresh read-only review task, one short request is sufficient:

`Проведи code review CHANGE_ID.`

Invoke exactly:

```text
python3 <plugin-root>/scripts/dls.py --root <current-project> --json \
  review-run CHANGE_ID --kind code --stream
```

Wait for that exact process. `started` and `lane-transition` are non-terminal.
When using `functions.exec`, use the nested-session bridge in [cli.md](references/cli.md):
keep its JavaScript alive and poll every `tools.exec_command` `session_id` with
`tools.write_stdin` until the nested process exits. An outer `functions.wait`
may only resume that still-running JavaScript cell; it never replaces polling
the nested session. Do not discard the nested `session_id`, replace the wait
with `status`, start a second runner, create reviewer subagents, read raw model
output, or invent a verdict. A completed review requires a non-null
`review_result_path`.

Routing is fixed:

- routine/standard: one Terra/high structured analysis;
- critical: Terra/high plus one Sol risk reviewer only for trust, data,
  reliability, or contract triggers;
- reconciliation only for a direct structured contradiction;
- invalid JSON/reference structure receives one compact repair without source.

Show findings from the canonical result. The skill may format their stored
locations as inline comments; the core stores no presentation directives.
`review-clear` is not acceptance.

## Finish

- After `not-clear`, recommend a fresh implementation task:
  `Исправь findings последнего review CHANGE_ID.`
- After `review-clear`, ask the user to accept the exact reviewed HEAD and
  definition digest. Record `approve --decision accept` only after a direct
  affirmative answer.
- Show `status --details receipt` after review or acceptance.
- Report implemented, validated, review-clear, accepted, release, and production
  separately. DLS stops at accepted; release/production remain explicit external
  boundaries.

## Boundaries

- No mandatory Plan Mode, brainstorming, TDD ritual, subagent workflow, or
  per-ticket reviewer.
- No caller-supplied operation IDs, pack paths, evidence paths, or state revisions.
- Never execute commands copied from Markdown or model output.
- Legacy state v1 is handled only by `upgrade`; legacy ReviewPack/ReviewIR is
  archived evidence and never executable runtime input.
