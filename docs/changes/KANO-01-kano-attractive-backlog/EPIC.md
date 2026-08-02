# KANO Attractive backlog — Epic

ID: `KANO-01`

## Product outcome

Roadmap records 50 opt-in Attractive candidates A11-A60 with priorities, before-after behavior, promotion signals, and no runtime change.

## Scope and deliverables

- Add one non-active `Attractive backlog` section to `docs/roadmap.md`.
- Register exactly 50 new optional KANO candidates: `A11` through `A60`.
- Group candidates by promotion priority: `P1` pilot now, `P2` after several
  real pilots, and `P3` only after a repeated signal.
- Give every candidate a concise `Было` and `Будет` description.

## Non-goals

- Runtime, CLI, schema, hook, profile, dependency, connector, or state-machine
  changes.
- Any implementation ticket for a candidate feature.
- Rewriting the frozen historical KANO snapshot or reclassifying `A01–A10`.
- Automatic approvals, task creation, global configuration changes, or
  autonomous delivery.

## Success measures

- `A11–A60` appear once each in the live Roadmap, with no gap or duplicate.
- Every candidate is explicitly optional and has a promotion signal.
- The public-repository validator passes.

## Dependencies

- None.

## Epic acceptance

- `REQ-001`: The Roadmap contains all 50 defined candidates.
- `REQ-002`: The Roadmap separates the candidates from active commitments.
- `REQ-003`: The change makes no runtime or historical-catalog claim.

## Risk rationale

Control level: standard.
