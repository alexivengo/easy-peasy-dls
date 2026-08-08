# bx-dev mechanics adoption — Epic

ID: `BX-00`

Status: draft; no DLS state, independent review, or human approval.

## Product outcome

DLS adopts four engineering disciplines distilled from the public `bx-dev` skill
without adopting its virtual-team model: a closed-world routing registry,
fail-closed handling for unknown routing values, documented risk-trigger
rationale, and an explicit reversibility invariant.

## Scope and deliverables

- Add one closed-world routing registry in `plugins/dls/scripts/dls_core/`
  for review verdicts, runner states, stream events, and `next_action.id`.
- Make every consumer of those values route through the registry; unknown
  values become typed integrity failures and do not mutate DLS state.
- Document the risk-trigger taxonomy with explicit included, intentionally
  excluded, and reason lists, in `docs/technical-reference.md` and the
  runner module that owns the mapping.
- State the reversibility invariant (`reversible → gate → irreversible`) in
  `docs/technical-reference.md` and add one regression test.
- Add tests covering unknown verdict, unknown `next_action.id`, risk-trigger
  positives and near-miss negatives, the reversibility invariant, and the
  absence of destructive rollback paths.

## Non-goals

- Importing `bx-dev` session-branch workflow, `dev` integration branch, or
  PR-merge lifecycle.
- Importing the Lead / Dev / Bug Reviewer / Security Reviewer / Compliance
  Reviewer / QA / Merger roles or the pre-spawn agent lifecycle.
- Importing the `--solo` / `--careful` / `--plan-approve` / `--no-review` /
  `--sop` / `--no-sop` modes.
- Importing the SCOUT Report template, complexity routing
  (`TRIVIAL / MODERATE / COMPLEX`), filename security regex, or
  `type(dev)` Conventional Commit policy.
- Adding release / production / merge-conflict subsystems.
- Touching `assets/profiles/*.toml` to express routing or rationale.
- Any new dependency, remote service, global configuration, telemetry, or
  automatic approval.

## Success measures

- Routing registry exists as the single source of truth and is referenced
  from every consumer site.
- Unknown routing value produces a typed integrity failure with the
  recognized list; DLS state is unchanged and no destructive fallback runs.
- Risk-trigger rationale is visible from `docs/technical-reference.md` and
  matches the mapping in `runner.py`; profiles remain advisory only.
- The reversibility invariant is stated in `docs/technical-reference.md` and
  is enforced by one regression test.
- All five test categories in `TICKETS.md` pass.
- Public repository validator passes.

## Dependencies

- None.

## Epic acceptance

- `REQ-001`: A single closed-world routing registry owns review verdicts,
  runner states, stream events, and `next_action.id`; consumers do not
  string-match these values.
- `REQ-002`: Unknown routing values produce a typed integrity failure that
  preserves state and never invokes a destructive fallback.
- `REQ-003`: Risk-trigger rationale lists the included triggers, the
  intentionally excluded triggers, and the reason; the rationale lives in
  `docs/technical-reference.md` and the runner module, not in profiles.
- `REQ-004`: The reversibility invariant is documented and one regression
  test enforces it.
- `REQ-005`: No `bx-dev` element listed in *Non-goals* is imported by this
  change.

## Risk rationale

Control level: standard.
