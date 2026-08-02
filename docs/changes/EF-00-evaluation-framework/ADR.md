# ADR-001 — Evaluation framework MVP boundary

## Context

Easy Peasy DLS already has deterministic `unittest` fixtures, fake Codex
executables, DLS receipts/metrics, a bounded review runner, and a public
validator. The framework needs reproducible evidence that these components
prevent the intended failures, without duplicating the delivery lifecycle or
collecting private model trajectories.

## Decision

MVP evaluation is a three-layer process:

1. L0 runs the existing deterministic tests and public validator with zero
   model calls. It maps HC-01 through HC-05 to exact executable tests.
2. L1 is release-only: four frozen semantic ReviewPack cases use an explicit
   arm manifest. Component-off runs are causal and change one component only;
   current-versus-previous-release runs detect regressions but make no causal
   attribution because several components may differ. It has an explicit
   live-call budget.
3. L2 records bounded, private field/iOS observations in a Markdown decision
   log. It never writes raw transcripts, repository paths, or trajectories into
   DLS state.

The M1 implementation uses existing tests plus `docs/evaluation-decisions.md`.
No new runner, JSONL ledger, database, dashboard, service, MCP evaluator, or
Harbor dependency is part of the MVP.

## Alternatives

- Build a generic evaluator and JSONL store now: rejected. There is no
  demonstrated automation trigger, and it would duplicate existing evidence.
- Add Harbor now: rejected. It is an optional container-first semantic-eval
  spike only after M2 and a measured scale trigger; it cannot represent Codex
  App hooks or native iOS runtime evidence.
- Use an LLM judge for hard claims: rejected. Git/state/test oracles are
  deterministic and have a clear failure boundary.

## Consequences

The first follow-up change is limited to the claim-to-test map, any genuinely
missing L0 regression proofs, and the decision log. Automation is allowed only
after two manual release evaluations exceed 30 minutes, more than four live
cases exist, more than two arms are needed, two transcription errors occur, or
a machine-readable Harbor/backend interchange is required.

The framework preserves DLS as the only lifecycle source of truth. Release and
production remain external decisions; an eval result cannot grant either.

## Supersession

None.
