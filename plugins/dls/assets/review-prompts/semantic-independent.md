# DLS independent semantic review

Review change `{{CHANGE_ID}}`, review ID `{{REVIEW_ID}}`, using the `{{PASS_KIND}}`
pass. Read `.dls-review-input/context.json`, the bound ReviewPack, and every input
named by the context. Inspect the exact Git range
`{{COMPARISON_BASE_SHA}}..{{HEAD_SHA}}`.

Use ticket IDs exactly as listed here: `{{CANONICAL_TICKET_IDS}}`. Required
prior finding IDs are `{{REQUIRED_PRIOR_FINDING_IDS}}`. Do not abbreviate or
invent either kind of identifier. Copy requirement IDs exactly from bound
authored inputs.

For every required prior finding, return exactly one verdict. `verified` and
`waived` require `replacement_finding_id: null`. `still-open` and `regressed`
require a new complete finding with a different ID, and
`replacement_finding_id` must equal that new finding ID and must differ from
the prior finding ID.

For a targeted pass, independently adjudicate every required prior finding,
the remediation delta, affected paths, blast radius, and current evidence. For a
full pass, review the whole epic range, all tickets, cross-ticket integration,
contract conformance, and validation gaps.

Do not search for or read native review output, specialist output, or another
semantic draft. Do not modify any file. Return only JSON matching
`.dls-review-input/output.schema.json`. DLS derives canonical ticket verdicts
from finding links, severity, and review-stage blockers; do not spend analysis
on that mechanical aggregation.
