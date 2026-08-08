# bx-dev mechanics adoption — Specification

ID: `BX-00`

Status: draft; no DLS state, independent review, or human approval.

## Problem and outcome

A review of the public `bx-dev` skill identified four engineering disciplines
that would tighten DLS routing and prevent silent inference. DLS must adopt
those disciplines without absorbing the virtual-team harness the skill is
built around. The four disciplines are:

1. A closed-world routing registry for review verdicts, runner states,
   stream events, and `next_action.id`.
2. Fail-closed handling for any value outside the registry.
3. Documented rationale for the risk-trigger taxonomy, including which
   triggers are intentionally excluded.
4. An explicit `reversible → gate → irreversible` invariant.

After this change, every DLS routing decision must come from one registry,
unknown values must stop the workflow with a typed diagnostic, and the
rationale behind the risk-trigger mapping must be visible to a reader of
`docs/technical-reference.md` without reading the source.

## Scope

- Add one closed-world routing registry module under
  `plugins/dls/scripts/dls_core/routing.py` that exposes frozen enums or
  `frozenset` constants for the four value families.
- Re-route every consumer in `runner.py`, `core.py`, CLI, skill, and hooks
  through the registry; remove direct string literals used as routing keys.
- Add one `route_unknown` helper that raises a typed
  `DlsIntegrityFailure` carrying the field name, the offending value, and
  the recognized list. The helper must not mutate state and must not
  silently coerce the value to a known one.
- Document the risk-trigger taxonomy in
  `docs/technical-reference.md` with three named lists: included,
  intentionally excluded, and reason. Update the runner module header with
  the same lists.
- Add the reversibility invariant as one paragraph in
  `docs/technical-reference.md` and one regression test under
  `plugins/dls/tests/`.

## Non-goals

- Importing any `bx-dev` element listed in `EPIC.md` *Non-goals*.
- Modifying `assets/profiles/*.toml` to express rationale, routing, or
  gates.
- Adding release / production / merge-conflict subsystems.
- Persisting raw model transcripts, raw verdicts, or new telemetry.
- Adding new external dependencies.
- Touching state schema versions, ReviewPack versions, or ReviewIR
  schemas.

## Current-system discovery

- `docs/technical-reference.md` already fixes public runner states
  (`not-prepared`, `running`, `completed`, `blocked`, `failed`) and the
  terminal-event rule (`completed` with `terminal=true`).
- `plugins/dls/scripts/dls_core/runner.py` already owns the risk-trigger
  mapping (`auth / security-privacy`, `data-loss / data-migration`,
  `concurrency / availability`, `public-api / compatibility`) and the
  secondary-reviewer allocation.
- `plugins/dls/scripts/dls_core/core.py` already uses
  `atomic_write_json` and preserves backup sidecars; the reversibility
  invariant must not be confused with that backup mechanism.
- CLI commands and flags are already fail-closed by `argparse`; the new
  routing registry is about routing values, not CLI arguments.
- The current implementation uses `routine / standard / critical` plus
  independent definition, architecture, design, and acceptance gates; the
  change does not add new control levels or new gates.

## Requirements and acceptance

- `REQ-001`: One routing registry module exposes frozen collections for:
  - review verdicts;
  - runner states;
  - stream events;
  - `next_action.id`.

  Every consumer reads from the registry. Direct string literals used as
  routing keys are replaced with named references.

- `REQ-002`: An unknown value in any of those four families produces a
  typed failure with this shape:

  ```json
  {
    "ok": false,
    "kind": "integrity",
    "error": "unknown-routing-value",
    "field": "<routing-family>",
    "value": "<offending-value>",
    "recognized": ["..."]
  }
  ```

  On this failure:

  - DLS state is not mutated;
  - no destructive Git action runs;
  - the recognized list is included in the diagnostic;
  - the model transcript is not displayed verbatim — the diagnostic
    carries only the offending value and the recognized list.

- `REQ-003`: The risk-trigger rationale in
  `docs/technical-reference.md` and the `runner.py` header lists exactly:

  ```text
  Included:
  - auth / security-privacy
  - data-loss / data-migration
  - concurrency / availability
  - public-api / compatibility

  Intentionally excluded:
  - architecture
  - release
  - external-dependency

  Reason: эти теги сами по себе не указывают на отдельный trust /
  data / reliability / contract risk и не должны автоматически
  тратить второй review lane.
  ```

  Profiles stay advisory and do not gain rationale, gates, models, or
  budgets.

- `REQ-004`: The reversibility invariant is documented as:

  > DLS выполняет сначала read-only и локально обратимые действия,
  > затем проверяемый gate, и только после него разрешает внешнее или
  > необратимое действие. Failure до gate не запускает cleanup и
  > destructive compensation.

  One regression test invokes an irreversible adapter before the human
  gate and asserts the call is rejected. A second test invokes a
  post-gate failure and asserts state and evidence are preserved and no
  `git reset` / `git stash` / `git revert` runs automatically.

- `REQ-005`: No element from `EPIC.md` *Non-goals* appears in code, docs,
  CLI, profiles, or hooks added by this change. The PR diff is grepped
  before merge against the forbidden list:

  ```text
  session-branch, --solo, --careful, --plan-approve, --no-review,
  --sop, --no-sop, scout-report, type(dev), marketing/, webapp-testing,
  audit-website, DDD, bounded-context, aggregate root
  ```

<!-- dls:architecture:start -->
## Architecture and alternatives

One routing registry module + documentation updates + tests. No new
subsystem, no new state, no schema bump. Rejected alternatives: copying the
`bx-dev` session-branch model, copying the Lead/Dev/Reviewer/Merger roles,
or moving rationale into `assets/profiles/*.toml`.
<!-- dls:architecture:end -->

## Interfaces, state, and failure behavior

- New module: `plugins/dls/scripts/dls_core/routing.py`. Public symbols:
  `ReviewVerdict`, `RunnerState`, `StreamEvent`, `NextAction`,
  `route_unknown(field, value)`. Each is a frozen collection or a small
  enum-like class; values are exported by name so consumers reference them
  symbolically.
- No new persisted state, no schema change, no CLI surface change.
- Failure shape is the JSON literal from `REQ-002`. The helper raises
  `DlsIntegrityFailure`; CLI and skill render the diagnostic without
  leaking the original model output.

## Security, privacy, data, and operations

- No new data collection, telemetry, connector, secret, or remote service.
- No new permission, scope, or auth model.
- No operational mutation beyond the existing CLI, hooks, and skill files.

## UI/UX contract

<!-- dls:design:start -->
Mode: bypass
Rationale: this change edits Python modules, tests, and a Markdown
reference. It introduces no product UI.
<!-- dls:design:end -->

## Validation intent

- Run the closed-world tests listed in `TICKETS.md` `T04`.
- Run the public repository validator.
- Diff-grep the change against the forbidden list in `REQ-005`; the PR
  must contain zero hits.
- Re-run the existing review runner on a routine, a standard, and a
  critical change to confirm no behavioral drift.

## Risk rationale

Control level: standard.
