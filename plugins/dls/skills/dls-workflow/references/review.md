# DLS code review

Use this procedure only in a separate review task. The task may start in the main Codex project or the ReviewPack owner checkout. Product source is read-only; only owner-local ignored `.dls/cache/` drafts and the canonical imported ReviewIR may be written.

## 1. Start the bound review

Run:

```text
dls --json review-start CHANGE_ID --operation-id <stable-id>
```

Do not run generic `doctor`, `init`, or `adopt`; search siblings; infer branches; or switch worktrees. DLS may resolve only current state, an explicit valid worktree registration, or a user-confirmed absolute pack. Without `--pack`, it selects only an unfinished pack for the current HEAD. If none exists after an imported review, it runs repeat `review-ready` and prepares the remediation pack itself. An explicit `--pack` never enables auto-preparation.

If `review-start` returns `ok: false`, stop and report its single `next_action`. `provide-review-base` means the implementation task did not prepare the first review handoff. Never use a stale pack or infer a branch to bypass that boundary.

Stop on any preflight or native-lane failure. Never substitute an improvised diff review.

Treat returned `owner_root` as the root of every later read, draft, and CLI mutation. `review-start` validates the pack, approval/definition, exact revisions, source snapshot, tickets, and evidence, then runs official non-interactive native review with Terra/high when required. Its `native.output_path` is the bounded final result; `native.transcript_path` is diagnostic only.

Use the returned Sol model/effort. Standard review is Sol/high; critical concurrency, security, or auth review is Sol/xhigh.

## 2. Run deterministic specialist lanes

Run one independent read-only specialist for every returned `risk_lenses` entry, in the listed order, and no others. There are at most three:

- `contract-trust`;
- `concurrency-reliability`;
- `data-migration`;
- `ux-interaction`;
- `architecture-integration`.

Each specialist reads the review manifest inputs and its named lens, does not read native output or another specialist draft, and writes a bounded structured draft under:

```text
.dls/cache/reviews/CHANGE_ID/REVIEW_ID/specialist-<lens-id>.json
```

Specialists advise the semantic reviewer; they do not create canonical findings, approve the change, or mutate source. If no risk lens is returned, spawn none.

## 3. Run the semantic algorithm

Read the review context manifest and every input it names. Do not read `native.output_path` until the independent semantic draft for the applicable first pass exists.

### Full review

Review the full `epic_base_sha..head_sha`, all tickets, cross-ticket integration, contract conformance, and evidence. Write a `full` independent draft under owner-local cache. Then read specialist drafts and native final output, verify every observation, reject false positives, add omissions, and build one canonical candidate.

### Remediation review

First run a `targeted` pass over:

- every `required_prior_findings` entry;
- the `comparison_base_sha..head_sha` delta;
- the bound remediation manifest;
- affected paths and blast-radius triggers;
- new/current validation evidence.

Treat ReviewPack `required_prior_findings` as the canonical current finding set. A legacy remediation manifest remains immutable implementation-time evidence and may contain older compatibility history; use it for provenance and blast radius, not to resurrect findings absent from the pack.

For every prior actionable finding, record exactly one verdict:

- `verified`;
- `still-open` with a replacement finding ID;
- `regressed` with a replacement finding ID;
- `waived`, matching the scoped human waiver.

Write the targeted draft before reading native output. Then reconcile specialist and native observations.

If a prior/new blocker remains, import `not-clear` after the targeted pass; do not spend a final-full pass. If the targeted result has no review-blocking blocker, run exactly one `final-full` semantic pass in the same task over `epic_base_sha..head_sha`. Repeat `review-clear` requires that final pass and independent verification or waiver of every prior actionable finding.

## 4. Build ReviewIR v2

Write ReviewIR under owner-local ignored cache. Bind:

- schema version 2, review/change IDs, review mode, exact epic/comparison/head SHAs;
- pack and definition digests;
- native metadata and the exact continuous `coverage_chain` returned by `review-start`;
- semantic context, model/effort, independent draft, and ordered `passes`;
- specialist draft paths/digests for exactly the returned risk lenses;
- one ticket verdict for every pack ticket;
- one prior-finding verdict for every required prior finding;
- canonical findings and an aggregate verdict consistent with them.

Every finding includes affected `ticket_ids`, `requirement_ids`, exact base/head, and one or more blocked stages: `review`, `acceptance`, `release`, `production`. Omitted `blocks` is legacy-only and means review plus acceptance. Release/production-only gaps do not prevent `review-clear` unless the ticket contract places them earlier.

Treat a prior `note` disposition as an implementer challenge, not closure. Independently verify it and record a required prior-finding verdict. If an external gap remains but belongs only to release or production, verify the old review-stage finding and emit a new release/production-only finding. Any external finding that blocks review or acceptance must cite the exact definition or ticket clause that places the evidence at that stage.

## 5. Import atomically

Run dry-run and real import with the current state revision:

```text
dls --root OWNER_ROOT --json review-import CHANGE_ID .dls/cache/.../reviewir.json \
  --expect-revision REVISION --operation-id <stable-id> --dry-run

dls --root OWNER_ROOT --json review-import CHANGE_ID .dls/cache/.../reviewir.json \
  --expect-revision REVISION --operation-id <stable-id>
```

For `not-clear` or `blocked`, the CLI may return nonzero after a successful import. Completion requires JSON with the expected verdict and non-null `review_result_path`.

Import is the only authority that turns an implementer’s `addressed` finding into `verified`. It also rejects incomplete prior verdicts, broken native coverage, missing final-full clearance, source/HEAD/definition/pack drift, or draft-integrity failure.

## 6. Report the boundary

Verify product source still matches the pack snapshot. Report findings first, then prior-finding verdicts, ticket verdicts, integration, validation gaps, and exact imported result path.

Never finish without `review_result_path` and never record human approval. Keep real-client transcripts, signing/notarization, deployment, and production proof outside code-review blockers unless the reviewed ticket explicitly requires them for implementation acceptance.
