# DLS remediation final-full review

The independent targeted review and native delta for review `{{REVIEW_ID}}`
are clean. Perform one final whole-epic semantic pass over
`{{EPIC_BASE_SHA}}..{{HEAD_SHA}}`.

Use ticket IDs exactly as listed here: `{{CANONICAL_TICKET_IDS}}`. Required
prior finding IDs are `{{REQUIRED_PRIOR_FINDING_IDS}}`. Do not abbreviate or
invent either kind of identifier. Copy requirement IDs exactly from bound
authored inputs.

For every required prior finding, return exactly one verdict. `verified` and
`waived` require `replacement_finding_id: null`. `still-open` and `regressed`
require a new complete finding with a different ID, and
`replacement_finding_id` must equal that new finding ID and must differ from
the prior finding ID.

This pass runs in an input-only workspace without a product checkout. Read only
the immutable files under `.dls-review-input/`: the bound context and ReviewPack,
`epic.patch`, `coverage.json`, `budget-plan.json`, native output, independent
targeted draft, specialist results, and `targeted-decision.json`. Treat
`coverage.json` as the exact whole-change path inventory and inspect every path
represented by `epic.patch`. Re-check every ticket, cross-ticket integration,
contract conformance, blast radius, and current validation evidence. The
returned decision supersedes the targeted decision.

Use no more than {{FINAL_FULL_COMMAND_TARGET}} inspection command invocations.
The runtime hard ceiling is {{FINAL_FULL_COMMAND_CEILING}}; crossing it stops
the lane before JSON can be returned. Batch related reads and reserve enough
headroom to finish the required JSON decision.

Do not modify files. Return only JSON matching
`.dls-review-input/output.schema.json`. DLS derives canonical ticket verdicts
from the returned findings; keep review, acceptance, release, and production
stages distinct.
