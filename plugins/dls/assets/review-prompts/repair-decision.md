# DLS decision-reference repair

Repair the JSON decision described by `.dls-review-input/repair.json` without
reviewing code or changing its semantic conclusions. Preserve the original
verdict, summary, prior verdicts, evidence, and existing findings except where
the supplied `validation_errors` require reference repairs. Resolve every
listed error in this one output; do not stop after the first error.

Use every entry in `reserved_replacement_ids`, and use no other new ID. For a
`still-open` or `regressed` prior finding, add one complete replacement finding
and point `replacement_finding_id` to it. Copy its `severity`, `kind`,
`ticket_ids`, `requirement_ids`, and `blocks` exactly from the corresponding
canonical prior finding. For `verified` or `waived`, keep
`replacement_finding_id` null. Never reuse the prior finding ID. For any other
reference error, change only the invalid reference field and use only the
canonical IDs listed in `repair.json`.

Return only JSON matching `.dls-review-input/output.schema.json`.
