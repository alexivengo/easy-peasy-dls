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
- Show the DLS-provided `human_decision` card once. List every pending decision
  and short digest, then ask its exact `Да / Нет` prompt. The user never copies
  identifiers; on `Да`, pass the hidden decision ID and verbatim response to one
  atomic approval command. On `Нет`, stop without mutation.
- A dependency only blocks implementation and always means
  `OTHER_CHANGE accepted-in-base`. It never blocks earlier definition work.

## Implementation and remediation

- Before reading or changing product files, invoke `status CHANGE_ID` from the
  project the user opened. Read its `execution_context`; do not start repository
  discovery first.
- Use `owner_root` as the working directory for every subsequent read, edit,
  test, commit, and DLS call. A dirty caller outside the owner is left untouched.
- On `prepare-owner-worktree` or `bind-owner-worktree`, invoke `worktree prepare
  CHANGE_ID` without inventing a base, then read status once more. Git identity,
  not branch naming, owns the change.
- On `commit-owner-source`, do not touch the draft before permission. Ask once,
  exactly: `Продолжить существующий черновик? Да / Нет.`
- If the immediately following user response is `Да`, preserve every existing
  change, inspect the diff, use `owner_root`, and continue the non-terminal
  implementation loop without asking again for that draft. On `Нет`, stop.
- Stop on missing, ambiguous, divergent, or cross-repository owner conflicts.
  Never stash, reset, transfer, delete, overwrite, or merge an uncommitted
  draft automatically. Draft permission authorizes continuation only.
- Use the single lifecycle `next_action` to select the current stage; do not
  inspect state internals or reconstruct history.
- Implement the accepted scope, run focused tests while coding, commit the
  candidate, then invoke only `candidate-ready`.
- If DLS already has a candidate lineage, do not supply or replace its review
  base. Invoke `candidate-ready` without `--base`; DLS reuses the preserved base.
- On remediation, read current findings via `status --details findings`.
  Declare each as `--address` or `--note`; never set `verified`.
- A clean intermediate remediation commit is a checkpoint, not a candidate
  boundary. Continue in the same owner until every current actionable finding
  has a deliberate disposition, then invoke `candidate-ready` once.
- Never use `--note` for unfinished work. It is only for a genuine dispute or
  independent reclassification that the next reviewer must adjudicate.
- Treat `continue-implementation`, `remediate-findings`, `run-candidate-ready`,
  `fix-validation`, and `wait-candidate` as non-terminal. Execute the action;
  never send a progress-only final response or ask the user to say `continue`.
- After each checkpoint commit, read `status` again and stay in the loop. Split
  large findings into internal steps without turning them into user handoffs.
- `candidate-ready` runs repository-owned required commands, records exact-HEAD
  evidence, and prepares the current ReviewPack. Stop at `open-review-task`.
- End an implementation task only at `open-review-task`, a human decision, an
  external dependency/workspace conflict, or a proven integrity/infrastructure
  blocker. A fixable validation failure is not terminal.
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
- critical: Terra/high plus one Sol risk reviewer only when the primary is
  clean and trust, data, reliability, or contract risk requires independent
  clearance;
- any primary blocker or should-fix immediately imports canonical `not-clear`;
  do not launch the optional reviewer or reconciliation afterward;
- reconciliation only for a direct structured contradiction;
- invalid JSON/reference structure receives one compact repair without source.

Lane budget targets are telemetry allocations, not reasons to discard a valid
decision. A valid actionable result remains `not-clear`; aggregate overrun can
never produce `review-clear`. Reinvoke the same short review request only when
DLS returns `resume-review`; DLS must reuse stored lanes rather than start a new
analysis.

Show findings from the canonical result. The skill may format their stored
locations as inline comments; the core stores no presentation directives.
`review-clear` is not acceptance.

## Finish

- After `not-clear`, recommend a fresh implementation task:
  `Исправь findings последнего review CHANGE_ID.`
- After `review-clear`, show the card's change, short reviewed HEAD and short
  definition digest, then ask exactly: `Принять результат? Да / Нет.` On `Да`,
  invoke `approve --decision accept --decision-id <card-id> --response <answer>`;
  do not pass `--git-sha` or ask the user to repeat identifiers. On `Нет`, stop.
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
- Plugin upgrades are loaded only by fresh Codex tasks. Restart Codex and open
  a new task after reinstalling DLS; an already-open task keeps its old skill.
