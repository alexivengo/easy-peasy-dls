# DLS delivery paths

Select control depth from consequences, ambiguity, integration breadth, and reversibility, not document count.

| Path | Use when | Authored contract | Primary task shape | Required human decisions |
|---|---|---|---|---|
| Micro | Obvious, reversible, local, no durable contract | None | One task | None formal |
| Routine | Bounded bug, chore, or small feature with known approach | `CHANGE.md` | One task | Final acceptance; definition only if ambiguity remains |
| Standard | Material behavior or integration with a coherent solution space | `SPEC.md`; tickets only for multiple slices | Definition, implementation, independent review | Definition and final acceptance |
| Roadmap | Multiple coordinated deliverables or true epic container | `EPIC.md`, `SPEC.md`, `TICKETS.md` | Same three boundaries as standard | Definition and final acceptance |
| Critical | Expensive reversal, trust boundary, destructive migration, public contract, financial/data-loss risk, foundational concurrency | Full package; ADR only for a durable independent decision | Standard plus at most one early architecture decision | Conditional architecture decision, definition, acceptance |
| Spike | Time-boxed uncertainty reduction | Compact `CHANGE.md` | One task | Result disposal or productionization |
| Hotfix | Urgent incident containment | Compact `CHANGE.md` | One or two tasks | Emergency exception and retrospective acceptance/review |

Impact tags select controls or domain specialists. A tag alone does not automatically force critical.

Examples:

- Copy correction using an exact existing screen: routine + `user-interface`, design precedent recorded.
- New account recovery flow: standard + `auth` + `user-interface`.
- Irreversible cross-service data migration: critical + `data-migration` + `data-loss` + `architecture`.
- Localized flaky test with known cause: routine bug.
