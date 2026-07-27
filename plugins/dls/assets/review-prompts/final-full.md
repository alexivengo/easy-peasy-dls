# DLS remediation final-full review

The targeted reconciliation for review `{{REVIEW_ID}}` found no blocker. Perform
one final whole-epic semantic pass over `{{EPIC_BASE_SHA}}..{{HEAD_SHA}}`.

Use ticket IDs exactly as listed here: `{{CANONICAL_TICKET_IDS}}`. Required
prior finding IDs are `{{REQUIRED_PRIOR_FINDING_IDS}}`. Do not abbreviate or
invent either kind of identifier. Copy requirement IDs exactly from bound
authored inputs.

Read the bound context, ReviewPack, native output, independent targeted draft,
specialist results, and `.dls-review-input/targeted-decision.json`. Re-check every
ticket, cross-ticket integration, contract conformance, blast radius, and current
validation evidence. The returned decision supersedes the targeted decision.

Do not modify files. Return only JSON matching
`.dls-review-input/output.schema.json`. DLS derives canonical ticket verdicts
from the returned findings; keep review, acceptance, release, and production
stages distinct.
