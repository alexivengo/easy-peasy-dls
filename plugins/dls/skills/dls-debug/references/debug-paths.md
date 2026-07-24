# DLS debug paths

| Path | Typical bug | Required shape |
|---|---|---|
| Micro | Obvious typo, assertion message, or reversible local correction with no durable behavior choice | Reproduce or verify premise, patch, focused check |
| Routine | Localized product bug with bounded impact and understood architecture | Compact `CHANGE.md`, reproduction, root cause, fix, regression proof, evidence, acceptance |
| Standard | Material behavior, multiple modules, public interface, meaningful UI flow, or ambiguous solution | SPEC, optional tickets for multiple slices, definition approval, implementation, independent review, acceptance |
| Critical | Trust-boundary flaw, destructive migration, plausible data loss or financial harm, foundational concurrency/consistency failure, expensive irreversible recovery | Full package, conditional early architecture decision, exact-revision gates |
| Hotfix | Active incident where containment cannot wait for full analysis | Compact incident change, explicitly authorized exception, containment evidence, then RCA and retrospective review/acceptance |

Escalate control for consequences, ambiguity, breadth, and reversibility. Do not escalate merely because the stack trace is long or many files are nearby.

Evidence depth:

- Reproduction must exercise the reported failure mode or clearly state why it cannot.
- Regression proof must synchronize on the behavior being asserted; timing luck is not proof.
- Environment/runtime gates remain distinct from local unit tests.
- A dependency or infrastructure blocker is not a product defect unless evidence connects it.
