# DLS routine review

Review the exact diff for change `{{CHANGE_ID}}` at `{{HEAD_SHA}}` against
`{{COMPARISON_BASE_SHA}}`. This is the only independent model pass for a routine
change, so inspect correctness, regressions, tests, and the bounded blast radius.

Use ticket IDs exactly as listed here: `{{CANONICAL_TICKET_IDS}}`. Required prior
finding IDs are `{{REQUIRED_PRIOR_FINDING_IDS}}`; their bounded canonical details
are `{{CANONICAL_PRIOR_FINDINGS}}`. Do not abbreviate or invent identifiers. For
every required prior finding return exactly one verdict:
`verified` and `waived` require `replacement_finding_id: null`; `still-open` and
`regressed` require a new complete finding with a different ID and a matching
`replacement_finding_id`.

Return `review-clear` only when no blocker or should-fix remains. Do not modify
files. Return only JSON matching the supplied output schema. If this Codex CLI
build applies its built-in review presentation after structured output, use only
this fallback grammar:

`review-clear: SUMMARY`

or one or more comments in the form:

`- [P1] TITLE — relative/path:LINE`
`  EXPLANATION AND REQUIRED FIX`

Use P0/P1 for blockers and P2/P3 for should-fix findings. Prefix TITLE with
`[PRIOR:FINDING_ID]` only when a prior finding remains or regressed.
