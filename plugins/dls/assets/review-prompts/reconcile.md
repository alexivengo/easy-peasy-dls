# DLS review reconciliation

Reconcile review `{{REVIEW_ID}}` for change `{{CHANGE_ID}}`.

Read the bound context and ReviewPack, then read:

- `.dls-review-input/native.txt`;
- `.dls-review-input/semantic-independent.json`;
- every specialist result under `.dls-review-input/specialists/`.

Verify every observation against the exact source at `{{HEAD_SHA}}`. Reject false
positives, merge duplicates, preserve independently supported omissions, and
produce one internally consistent decision. Every required prior finding needs
exactly one verdict. DLS derives canonical ticket verdicts from finding links,
severity, and stage blockers. Do not modify files. Return only JSON matching
`.dls-review-input/output.schema.json`.
