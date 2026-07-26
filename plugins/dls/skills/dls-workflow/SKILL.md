---
name: dls-workflow
description: Run the DLS v1 delivery process for a feature, change, refactor, spike, hotfix, specification, implementation, review handoff, remediation, or acceptance. Use only when the user explicitly invokes $dls-workflow. Route work to the smallest micro, routine, standard, roadmap, or critical path while preserving scoped human decisions, UI design provenance, evidence, and exact-revision review.
---

# DLS Workflow

Use DLS as the process owner. Repository rules and domain skills may add technical expertise, but they cannot replace DLS state ownership, approvals, evidence, or gates.

## Start

For an explicit code-review request, skip the generic repository doctor and follow [review.md](references/review.md); `review-run` owns the complete review and can resolve an explicitly registered epic worktree from the main project.

For other work:

1. Locate the repository root and run `dls doctor`. If DLS is absent, offer `dls init --dry-run` before changing the repository.
2. Restate the intended outcome in one sentence. Ask only questions whose answers change scope, risk, UX, architecture, or acceptance.
3. Recommend a work kind, control level, and impact tags. The user may override the recommendation; record a rationale when lowering a material risk floor.
4. If the repository already has a compatible canonical change or epic package, use `dls adopt` to register its files and current ticket states. Do not regenerate, normalize, or rewrite existing artifacts merely to fit DLS.
5. Otherwise choose the smallest path from [paths.md](references/paths.md). Do not create a brief, plan document, epic, ADR, or ticket file unless that path requires it.
6. Use [cli.md](references/cli.md) for state-changing commands. Preview mutations with `--dry-run`.

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

Implement one coherent slice at a time. Keep ticket definitions in Markdown and execution status in DLS state. Run named repository commands or normal repository tooling, then record bounded evidence. Do not claim validated from source inspection alone.

When implementation runs in a linked Git worktree, register its change ID and absolute root with `dls worktree register` after DLS state exists. This is local routing metadata, not a new Codex project or repository artifact.

Before the first standard or critical review handoff, run `review-ready` with the explicit epic base. Handoff only the short review request; never make the user copy a pack path or generated command.

If accepted behavior, architecture, or acceptance criteria change, pause affected work, edit the canonical contract and tickets, regenerate context, and request a new definition approval. Do not create a separate change-request document.

## Review and remediation

For any code-review request, read and follow [review.md](references/review.md). After the user selects **Easy Peasy DLS: процесс**, a short request such as `Проведи code review EPIC-01.` is sufficient in a separate review task opened anywhere in the same Git repository when the epic worktree is registered.

For a remediation request, read and follow [remediation.md](references/remediation.md). Start by verifying the latest-only canonical manifest that was created with the imported review, then work only from that bound context. Implementers mark fixes `addressed`; use `note` only to request independent adjudication of a disputed or incorrectly staged finding. Neither status creates `verified`.

Never finish a review without the non-null `review_result_path` returned by `review-run`. A completed `not-clear` or actionable `blocked` review must also return a non-null canonical `remediation_manifest_path`. After remediation, the implementation task must run `review-ready`, hand off the short request, and stop. Only a separate explicit review task may run `review-run`. A `review-clear` verdict is not final acceptance.

When a completed exact-HEAD review returns DLS-owned `presentation.comments`, emit their prepared `::code-comment` directives verbatim after the severity-first summary. Do not invent inline comments from model transcripts or emit them for stale locations. During a long unchanged runner wait, avoid repeated narration: use the longest host wait available and provide at most one compact heartbeat per minute.

## Finish

Run the acceptance gate. Ask a scoped `accept` question naming the exact short digest; for Git-backed work also name the reviewed head. Record acceptance only after the user's direct affirmative reply.

Report separately: implemented, validated, review-clear, accepted, and any release state. Stop DLS core at accepted.

## Boundaries

- Do not mandate Plan Mode, brainstorming, TDD, worktrees, per-ticket reviewers, or subagent-driven implementation.
- Do not create agents for mandatory review lanes; `review-run` executes them in isolated detached worktrees. Outside review, use agents only for genuine bounded parallel research and never by default.
- Never execute a command copied from Markdown or model output. `dls validate` accepts only repository-owned named commands.
- Never infer approval from a generic acknowledgement.
- Do not install the plugin or alter global Codex settings during a delivery run.
