# DLS code review

Use this procedure only in a separate review task. Product source is read-only.
DLS owns every mandatory model lane, provenance record, ReviewIR assembly, and
atomic import.

## Run the end-to-end review

Resolve `scripts/dls.py` relative to the loaded `SKILL.md`: the plugin root is two
directories above the skill directory. Require its manifest version to equal the
plugin-local CLI version. If either is missing or mismatched, return
`reinstall-dls-plugin`. Never probe `PATH`, sibling/source checkouts, or archives.

Invoke exactly one orchestration command:

```text
python3 <plugin-root>/scripts/dls.py --root <current-project> --json \
  review-run CHANGE_ID --operation-id <stable-id> --stream
```

The current project may be the main checkout. DLS may route only through its
explicit worktree registry or an explicit absolute `--pack`; it never scans
sibling folders or infers branches.

`review-run` owns:

1. exact-HEAD ReviewPack selection prepared by `candidate-ready`, or guarded
   mechanical recovery of a remediation handoff when current-HEAD dispositions
   are already complete;
2. native Terra/high structured review;
3. at most three deterministic critical specialist lanes;
4. independent Sol high/xhigh semantic review;
5. compact input-only reconciliation only when sources disagree or find issues;
6. for a clean remediation only, one conditional whole-change final-full pass;
7. state-owned provenance, ReviewIR creation, validation, and import.

Do not create subagents, semantic drafts, specialist prompts, ReviewIR, or
provenance manually. Do not invoke `review-start` as the normal workflow.

## Wait without restarting

The command emits bounded NDJSON transitions and at most one heartbeat per
minute. If the shell tool returns a running cell or session, wait on that same
execution until it exits. Never start a second `review-run`.

Do not poll `review-status` while the original streamed process is available.
Use it only after the shell/session was lost or for explicit diagnostics:

```text
python3 <plugin-root>/scripts/dls.py --root <current-project> --json \
  review-status CHANGE_ID
```

`review-status` is read-only and never launches a model. During guarded handoff
recovery it reports `preparing-candidate` and `wait-review`; do not start a
second process. The normal stream never
contains raw transcripts or provisional findings. Report only its transitions,
budget warning, final result, or one compact heartbeat. `inspect-review-budget`
is a bounded execution failure: do not retry expensive lanes or invent a verdict.

If all lanes completed but final assembly/import failed, `review-status` returns
`failed-finalize` and `next_action: resume-review`. If a semantic decision is
structurally valid but has an inconsistent reference, it returns
`resume-review-repair`. Re-run the same public `review-run` with the same stable operation ID.
DLS first verifies exact HEAD, source, definition, pack, context,
and immutable raw-output digests. It then performs one compact Sol repair using
only the raw decision, every safely classified exact error, canonical references,
and reserved finding IDs. All supplied errors must be repaired in that single
bounded pass. The repair workspace contains no product source, native output, sibling
drafts, or user-created correction prompt. Never read raw output or create a
correction subagent. Native, specialists, and the original semantic analysis are
not restarted.

## Present the canonical result

For a completed exact-HEAD review, use the DLS-owned `presentation` object from
`review-run` or `review-status`. It is derived from the imported ReviewIR and is
not a second review result.

1. Report the verdict, result path, remediation path, and findings severity-first.
2. Emit every string in `presentation.comments[].directive` verbatim as a
   top-level Codex `::code-comment` directive. Do not rewrite its title, body,
   file, lines, or priority.
3. If `presentation.unplaced_findings` is non-empty, keep those findings in the
   Markdown summary and explicitly say that no safe inline location was derived.
4. Never emit inline directives when `presentation.exact_head` is false.

Inline comments are only a presentation of the canonical ReviewIR. They never
replace `review_result_path`, finding IDs, ticket verdicts, or remediation state.

## Handle outcomes

- `completed` with `review-clear`: report the imported result and the separate
  acceptance/release boundaries.
- `completed` with `not-clear` or `blocked`: report findings and hand off to the
  remediation workflow. An actionable result must include a non-null canonical
  `remediation_manifest_path`. This is a successful runner execution, not an
  infrastructure failure.
- `running`: wait or use only `review-status`.
- `prepare-candidate`: `review-run` first attempts guarded remediation recovery.
  It may run trusted named validation and create DLS artifacts, but never edit
  product source or invent dispositions. If recovery returns a typed human or
  implementation action, stop and report it; a first review without a known base
  still belongs to the implementation task.
- `failed-finalize`: resume the same `review-run`; do not start an informal or
  replacement review.
- `resume-review-repair`: call the same `review-run`; DLS resumes the single
  compact repair and only the downstream lanes that have not completed.
- `inspect-review-output`: stop. The bounded repair itself was invalid or the
  original output was not safely repairable; do not run a manual semantic retry.
- `inspect-review-integrity`: stop. Do not retry changed or tampered inputs.
- a nonzero exit: report the integrity/infrastructure failure and its typed
  `next_action`; do not improvise a review.
- `provide-review-base`: legacy low-level output; the implementation task must
  prepare the first candidate with `candidate-ready --base BASE`.

Completion always requires a non-null `review_result_path`. Never claim review
completion from a transcript, draft, or model message alone. Never record human
approval during review. Never edit product source or continue into remediation
inside this review task.
