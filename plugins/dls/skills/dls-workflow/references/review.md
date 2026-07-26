# DLS code review

Use this procedure only in a separate review task. Product source is read-only.
DLS owns every mandatory model lane, provenance record, ReviewIR assembly, and
atomic import.

## Run the end-to-end review

Resolve `scripts/dls.py` relative to the loaded `SKILL.md`: the plugin root is two
directories above the skill directory. Never probe `PATH` for `dls`.

Invoke exactly one orchestration command:

```text
python3 <plugin-root>/scripts/dls.py --root <current-project> --json \
  review-run CHANGE_ID --operation-id <stable-id>
```

The current project may be the main checkout. DLS may route only through its
explicit worktree registry or an explicit absolute `--pack`; it never scans
sibling folders or infers branches.

`review-run` owns:

1. exact-HEAD ReviewPack selection or remediation-pack preparation;
2. native Terra/high review;
3. at most three deterministic critical specialist lanes;
4. independent Sol high/xhigh semantic review;
5. reconciliation and conditional remediation final-full review;
6. state-owned provenance, ReviewIR creation, validation, and import.

Do not create subagents, semantic drafts, specialist prompts, ReviewIR, or
provenance manually. Do not invoke `review-start` as the normal workflow.

## Wait without restarting

The command can run for up to 30 minutes per model attempt. If the shell tool
returns a running cell or session, wait on that same execution until it exits.
Never start a second `review-run`.

Do not wait for `review-run` to print `status: running`: its stdout is reserved
for the final machine-readable payload. After 20–30 seconds without completion,
keep the original shell/session alive and read progress from a separate shell
command:

```text
python3 <plugin-root>/scripts/dls.py --root <current-project> --json \
  review-status CHANGE_ID
```

`review-status` is read-only and never launches a model. Continue checking status
at a bounded interval regardless of whether the primary command has emitted any
stdout; do not replace the active review. Use its compact `progress` object and
do not request `--verbose` unless diagnosing an integrity failure. Report only a
lane/status transition or one compact unchanged heartbeat every 60–90 seconds,
for example `3/4 · semantic:final-full · Sol/xhigh · 8m`. Never stream raw model
transcripts or provisional findings.

If all lanes completed but final assembly/import failed, `review-status` returns
`failed-finalize` and `next_action: resume-review`. Re-run the same public
`review-run` with the same stable operation ID: it must reuse digest-bound
completed lanes and retry only deterministic finalization, never the models.

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
- `failed-finalize`: resume the same `review-run`; do not start an informal or
  replacement review.
- a nonzero exit: report the integrity/infrastructure failure and its typed
  `next_action`; do not improvise a review.
- `provide-review-base`: the implementation task must prepare the first candidate
  with `review-ready --base BASE`.

Completion always requires a non-null `review_result_path`. Never claim review
completion from a transcript, draft, or model message alone. Never record human
approval during review. Never edit product source or continue into remediation
inside this review task.
