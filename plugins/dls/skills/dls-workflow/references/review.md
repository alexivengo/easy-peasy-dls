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

The current project may be the main checkout. For an implicit pack selection,
the explicit worktree registry is authoritative even when the current checkout
still contains a portable or stale state copy. An explicit absolute `--pack`
remains the only one-off owner override. DLS never scans sibling folders or
infers branches.

`review-run` owns:

1. exact-HEAD ReviewPack selection prepared by `candidate-ready`, or guarded
   mechanical recovery of a remediation handoff when current-HEAD dispositions
   are already complete;
2. native Terra/high structured review;
3. at most three deterministic critical specialist lanes;
4. independent Sol high/xhigh semantic review;
5. compact input-only reconciliation only when sources disagree or find issues;
6. for a clean remediation only, one conditional whole-change final-full pass
   in a bounded input-only workspace with exact patch/coverage inventory;
7. state-owned provenance, ReviewIR creation, validation, and import.

Do not create subagents, semantic drafts, specialist prompts, ReviewIR, or
provenance manually. Do not invoke `review-start` as the normal workflow.

## Wait without restarting

The command emits bounded NDJSON transitions and at most one heartbeat per
minute. If the shell tool returns a running cell or session, wait on that same
execution until it exits. Never start a second `review-run`.

If the stream emits one `context-warning`, tell the user that this Codex task
has already served another DLS cycle or role and recommend a fresh task next
time. The warning is advisory: keep waiting on the same runner, do not restart
it, and do not replace its primary `next_action`. Do not repeat the warning on
heartbeats.

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
budget warning, final result, or one compact heartbeat. A target overrun inside
the state-owned recovery ceiling is recorded as a warning, not converted into a
false infrastructure failure. `inspect-review-budget` means the hard ceiling or
a non-token safety limit was exceeded: do not retry expensive lanes or invent a
verdict.

`resume-review-budget` means a completed structured output is already present
and is eligible for deterministic zero-call recovery. Re-run the same public
`review-run` with the same stable operation ID. DLS verifies HEAD, source,
definition, pack, raw output/transcript digests and original command/time/output
limits, then resumes only assembly/import. Never start another model lane.
`resume-review-command-budget` is narrower: a legacy final-full stopped just
above its old command limit but still below the installed hard ceiling. Re-run
the same public `review-run` with the same stable operation ID. DLS reuses every
completed upstream lane and permits exactly one new final-full attempt under
the changed bounded contract. The installed hard ceiling, timeout and
transcript limits remain terminal and return `inspect-review-budget`.
`split-review-scope` means the exact whole-change input bundle exceeds the
bounded final-pass contract; return to definition/implementation planning and
split the change instead of silently truncating coverage.

After a canonical import, stream mode emits exactly one bounded
`delivery-receipt` event immediately before `completed`. If the original stream
was lost and `review-status` confirms an imported result, invoke the read-only
`delivery-receipt CHANGE_ID` once. Never invoke it for a failed runner with no
canonical ReviewIR.

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

A legacy native `invalid-output` may also return `resume-review` when the raw
attempt completed before the installed DLS learned its deterministic recovery
contract. Re-run the same operation once. For standard/critical review, DLS may
verify the raw digest plus the JSONL transcript's final completed agent message,
record the prose as `native_decision_status: indeterminate`, and continue with
independent semantic reconciliation. This never means native clean and never
skips Sol adjudication. `inspect-review-output` means the transcript cannot
safely prove the output; `inspect-review-integrity` means immutable inputs or
digests drifted. Stop on either action and never launch another native review.

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
5. Show the returned `delivery_receipt` after the findings. For `review-clear`,
   use its Russian Markdown as the primary lifecycle summary before human
   acceptance. For `not-clear` or `blocked`, keep findings first, then the
   Receipt, then the one-line remediation handoff.

Inline comments are only a presentation of the canonical ReviewIR. They never
replace `review_result_path`, finding IDs, ticket verdicts, or remediation state.
The Receipt is likewise derived: it never replaces state, ReviewIR, evidence,
approval, release, or production gates and never requires a model call.

## Handle outcomes

- `completed` with `review-clear`: report the imported result and the separate
  acceptance/release boundaries.
- `completed` with `not-clear` or `blocked`: report findings and hand off to the
  remediation workflow using only `Исправь findings последнего review CHANGE_ID.`
  Prefer a fresh implementation task. An actionable result must include a
  non-null canonical `remediation_manifest_path`. This is a successful runner
  execution, not an infrastructure failure. Do not replay the manifest or
  findings in the handoff unless the user requests them.
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
- `resume-review-budget`: call the same `review-run`; DLS validates and imports
  the already completed output without a model call.
- `resume-review-command-budget`: call the same `review-run`; DLS preserves
  native/targeted/specialist/reconciliation provenance and retries only the
  legacy final-full once with its current bounded command contract.
- `split-review-scope`: stop and split the review scope; DLS did not truncate
  the whole-change coverage bundle.
- `resume-review` for legacy native invalid-output: call the same `review-run`
  once; DLS either recovers the completed output without a native model call or
  returns a terminal inspect action.
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
