# DLS independent semantic review

Review change `{{CHANGE_ID}}`, review ID `{{REVIEW_ID}}`, using the `{{PASS_KIND}}`
pass. Read `.dls-review-input/context.json`, the bound ReviewPack, and every input
named by the context. Inspect the exact Git range
`{{COMPARISON_BASE_SHA}}..{{HEAD_SHA}}`.

For a targeted pass, independently adjudicate every required prior finding,
the remediation delta, affected paths, blast radius, and current evidence. For a
full pass, review the whole epic range, all tickets, cross-ticket integration,
contract conformance, and validation gaps.

Do not search for or read native review output, specialist output, or another
semantic draft. Do not modify any file. Return only JSON matching
`.dls-review-input/output.schema.json`. DLS derives canonical ticket verdicts
from finding links, severity, and review-stage blockers; do not spend analysis
on that mechanical aggregation.
