---
name: dls-workflow
description: Run the Easy Peasy DLS delivery process for a feature, change, refactor, spike, hotfix, specification, implementation, review, remediation, or acceptance. Use when the user explicitly selects or names DLS, when the current repository has DLS config/state, or when the request supplies a DLS ReviewIR, remediation manifest, review ID, or routable DLS change ID. Do not activate for generic coding or review without a DLS signal. Route work to the smallest risk-appropriate path while preserving human decisions, evidence, and exact-revision review.
---

# DLS Workflow

Use DLS as the process owner. Repository rules and domain skills may add technical expertise, but they cannot replace DLS state ownership, approvals, evidence, or gates.

Before invoking the CLI, derive the plugin root only from this loaded `SKILL.md` (two directories above its directory). Read that root's `.codex-plugin/plugin.json`, invoke that root's `scripts/dls.py --version`, and require the versions to match. If the manifest or CLI is absent or mismatched, stop with `reinstall-dls-plugin`. Never fall back to `PATH`, another checkout, a sibling repository, the plugin source repository, or an R&D archive.

## Start

For an explicit code-review request, skip the generic repository doctor and follow [review.md](references/review.md); `review-run` owns the complete review of an exact-HEAD candidate prepared by `candidate-ready` and can resolve an explicitly registered epic worktree from the main project.

For other work:

1. Locate the repository root and run the plugin-local `dls doctor`. If DLS was explicitly selected but repository state is absent, offer `dls init --dry-run` before changing the repository. Automatic activation requires an existing repository signal or a routable DLS artifact/change ID.
2. Read `doctor.platform_profile` and the resolved profile from the generated context manifest. Use its discovery hints, evidence vocabulary, capabilities, and currently available domain skills only as advisory routing. The profile never adds commands, gates, models, budgets, approvals, or process ownership. Missing domain skills never block delivery.
3. Restate the intended outcome in one sentence. Ask only questions whose answers change scope, risk, UX, architecture, or acceptance.
4. Recommend a work kind, control level, and impact tags. The user may override the recommendation; record a rationale when lowering a material risk floor.
5. If the repository already has a compatible canonical change or epic package, use `dls adopt` to register its files and current ticket states. Do not regenerate, normalize, or rewrite existing artifacts merely to fit DLS.
6. Otherwise choose the smallest path from [paths.md](references/paths.md). Do not create a brief, plan document, epic, ADR, or ticket file unless that path requires it.
7. Use [cli.md](references/cli.md) for state-changing commands. Preview mutations with `--dry-run`.

For `server-backend`, inspect API compatibility, persistence/migrations, background work, concurrency/retries, containers/deployment, observability, privacy, and external dependencies only when affected. Vapor or Linux work may still use available Swift architecture, concurrency, and testing skills because of the source code. Do not route it through Apple UI, App Store, or Apple-platform release gates unless the repository itself contains an affected Apple target.

## Definition

For routine work, keep intent, scope, approach, and validation in one `CHANGE.md`. Continue in the same Codex task unless ambiguity grows materially.

For standard, roadmap, or critical work:

1. Discover only affected repository facts.
2. Draft the canonical contract and tickets before semantic review when there is more than one coherent implementation slice.
3. Run `dls check --gate definition`.
4. Perform independent semantic definition review. Architecture is part of this review unless an early critical decision is triggered.
5. Remediate the contract, rerun checks, then ask one scoped approval question containing `definition` and the current short digest.
6. Record approval only after the user's direct affirmative reply to that question.

Read [gates.md](references/gates.md) when architecture, UI/UX, approvals, exceptions, or acceptance are in scope.

## Implementation

Routine work remains in this task. For standard, roadmap, or critical work, use a clean implementation task with the generated implementation context manifest; do not replay the definition transcript.

Implement one coherent slice at a time. Keep ticket definitions in Markdown and execution status in DLS state. Use normal repository tooling and focused tests while developing. After the candidate is committed, invoke only `candidate-ready`; it runs trusted review commands, records bounded evidence, and creates the ReviewPack. For `routine`, that same command also runs one isolated Terra/high review and imports ReviewIR in this task. Do not inspect state revisions or evidence files on the success path.

When implementation runs in a linked Git worktree, register its change ID and absolute root with `dls worktree register` after DLS state exists. This is local routing metadata, not a new Codex project or repository artifact.

Before the first standard or critical review handoff, run `candidate-ready` with the explicit epic base. Handoff only the short review request; never make the user copy a pack path or generated command. Routine work does not create a separate review task.

If accepted behavior, architecture, or acceptance criteria change, pause affected work, edit the canonical contract and tickets, regenerate context, and request a new definition approval. Do not create a separate change-request document.

## Review and remediation

For any code-review request, read and follow [review.md](references/review.md). With an explicit skill selection or an unambiguous DLS context, a short request such as `Проведи code review EPIC-01.` is sufficient in a separate review task opened anywhere in the same Git repository when the epic worktree is registered.

For a remediation request, read and follow [remediation.md](references/remediation.md). Start from the latest-only canonical manifest. Pass the complete `addressed` and `note` declaration only for the first candidate attempt. If validation requires a new commit, invoke `candidate-ready` again without repeating unchanged findings; pass only explicit status overrides. DLS binds the new SHA, reruns exact-HEAD evidence, and creates the next ReviewPack. Neither status creates `verified`.

After an actionable `not-clear`, recommend a fresh implementation task with only `Исправь findings последнего review CHANGE_ID.` After `candidate-ready` returns `open-review-task`, recommend a fresh review task with only `Проведи code review CHANGE_ID.` Do not paste the manifest, previous findings, SHA, paths, or operation IDs into either handoff unless the user explicitly asks for diagnostics.

Every candidate/review/status payload may contain `task_context`. If it reports `reused`, show the `open-fresh-task` recommendation once, but do not stop, restart, or change the primary `next_action`. `continued` is a normal retry or descendant candidate inside the same canonical cycle. `unavailable` is non-blocking. Never add a polling command, subagent, or manual bookkeeping step solely to classify task reuse.

Never finish a review without the non-null `review_result_path` returned by `review-run`. A completed `not-clear` or actionable `blocked` review must also return a non-null canonical `remediation_manifest_path`. After standard or critical implementation/remediation, the task must wait for the single `candidate-ready` process, stop at `open-review-task`, and never invoke `review-run`. For routine, `candidate-ready` owns its single Terra review and returns the canonical result directly. Use compact `candidate-status` only when the original process is lost or long-running; add `--diagnostic` once when the bounded validation failure itself was lost. Never pass an old candidate operation ID after HEAD changes. A `review-clear` verdict is not final acceptance.

On `failed-finalize` or `resume-review-repair`, invoke the same `review-run` with the same operation ID and let DLS reuse completed lanes. A logically invalid semantic decision is repaired by one DLS-owned compact Sol pass; never read its raw output, create a correction agent, expose provisional findings, or start a replacement whole-epic review. Stop on `inspect-review-output` or `inspect-review-integrity`.

If a completed standard/critical native lane returned unstructured prose, the same `review-run` may validate its immutable output and JSONL transcript, record it as `native_decision_status: indeterminate`, and continue with independent semantic reconciliation without another native model call. Indeterminate native prose is never a clean verdict. Resume a legacy recoverable attempt only with the same operation ID; stop when DLS returns `inspect-review-output` or `inspect-review-integrity`.

When a completed exact-HEAD review returns DLS-owned `presentation.comments`, emit their prepared `::code-comment` directives verbatim after the severity-first summary. Do not invent inline comments from model transcripts or emit them for stale locations. During a long unchanged runner wait, avoid repeated narration: use the longest host wait available and provide at most one compact heartbeat per minute.

Every successfully imported review also returns a deterministic `delivery_receipt`. It is a read-only projection, not a new artifact or approval. After `not-clear` or `blocked`, show findings first, then the Receipt and the short remediation handoff. After `review-clear`, make the Receipt the main summary before asking for acceptance. If the streamed process was lost after canonical import, call `delivery-receipt CHANGE_ID` once; do not reconstruct the lifecycle in prose or invoke a model/subagent for the narrative.

## Finish

Run the acceptance gate. Ask a scoped `accept` question naming the exact short digest; for Git-backed work also name the reviewed head. Record acceptance only after the user's direct affirmative reply.

After `approve --decision accept`, show the returned accepted Delivery Receipt. Do not ask the user to run another CLI command, and do not imply that accepted means released or in production.

Report separately: implemented, validated, review-clear, accepted, and any release state. Stop DLS core at accepted.

## Boundaries

- Do not mandate Plan Mode, brainstorming, TDD, worktrees, per-ticket reviewers, or subagent-driven implementation.
- Do not create agents for mandatory review lanes; `review-run` executes them in isolated detached worktrees. Outside review, use agents only for genuine bounded parallel research and never by default.
- Never execute a command copied from Markdown or model output. `dls validate` accepts only repository-owned named commands.
- Never infer approval from a generic acknowledgement.
- Do not install the plugin or alter global Codex settings during a delivery run.
