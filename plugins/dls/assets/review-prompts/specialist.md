# DLS specialist review

Review change `{{CHANGE_ID}}`, review ID `{{REVIEW_ID}}`, through the single lens
`{{LENS_ID}}`: {{LENS_FOCUS}}

Use ticket IDs exactly as listed here: `{{CANONICAL_TICKET_IDS}}`. Do not
abbreviate or invent ticket IDs. Copy requirement IDs exactly from bound
authored inputs.

Read `.dls-review-input/context.json` and every input it names. Inspect the exact
Git range `{{COMPARISON_BASE_SHA}}..{{HEAD_SHA}}`. Do not search for or read any
native review output, semantic draft, or other specialist output.

Return only JSON matching `.dls-review-input/output.schema.json`. Report concrete,
actionable findings supported by the code. Do not approve the change and do not
modify any file.
