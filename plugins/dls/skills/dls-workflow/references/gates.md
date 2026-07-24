# DLS gates and human decisions

## Architecture

Architecture normally lives in the SPEC and is reviewed with the complete definition.

Insert one early focused architecture decision before completing the SPEC only when reversal is expensive, a public or cross-system contract changes, a trust boundary changes, financial or data-loss harm is plausible, a foundational concurrency/consistency model changes, or a durable alternative must be selected first.

Create an ADR only when the decision outlives the change, controls multiple deliverables or a public contract, governs a migration, or may be superseded independently.

When an ADR is created, ask a scoped `architecture` approval question with the current contract digest before definition approval. Later authored changes stale both records.

## UI and UX

Every `user-interface` change needs one of:

- an accepted exact existing screen, component, style, prototype, design file, image, PDF, or HTML source; or
- a scoped user decision to proceed without a sufficient source, naming affected surfaces and UX risk.

Tier 1 precedent changes may use an exact existing implementation reference. Tier 2 material composition needs versioned screenshots, a prototype/design file, or exact accepted precedent. Tier 3 new or complex experience needs a versioned immutable design artifact.

Cover only applicable states: flow, loading, empty, error, disabled, success, adaptive behavior, accessibility, localization, design-system use, and behavior-changing motion.

Record the source or bypass in the authored contract and the user's design decision in DLS state. Material source changes stale that decision and the definition approval.

## Scoped approval

Codex asks an immediately scoped question:

`Approve definition package 8fa21c?`

or:

`Accept implementation 8fa21c at head 91bc02e?`

Only a direct affirmative reply to that question may be recorded with `actor=codex`, `authority=user`, the prompt, and the response. A user may instead invoke the CLI directly with `actor=user`.

Any authored-content digest change stales definition and design approval. Any commit after review stales review-clear.

Generated Markdown regions, finding dispositions, evidence, and DLS state do not change the authored definition digest. Changing accepted behavior, architecture, a public contract, or acceptance criteria does.

## Acceptance

Routine acceptance requires current successful evidence. Independent review is optional, but if performed its latest result must be current and clear.

Standard and critical acceptance additionally require:

- current definition approval;
- accepted UI source or bypass when applicable;
- clean committed source;
- acceptance-grade review-clear for the current head;
- no open blocker;
- explicit risk acceptance for every open should-fix;
- current passing evidence.

ReviewIR findings declare which stage they block: `review`, `acceptance`, `release`, or `production`. Release-only and production-only gaps remain visible but do not block code-review clearance or acceptance unless their ticket contract explicitly places them in an earlier stage. Legacy findings without `blocks` are treated as blocking both review and acceptance.

An implementer `note` disputes applicability or stage but grants no waiver. It can make a committed candidate reviewable only so an independent ReviewIR can verify the old finding, keep it open, or replace it with correctly staged evidence.

Keep `implemented`, `validated`, `review-clear`, `accepted`, `release-ready`, `released`, and `production-verified` distinct.

Repository config may name required review and acceptance command IDs. Each required command needs its latest current PASS at that stage. Without a list, compatibility behavior requires at least one current PASS. Release-only commands are never promoted into review gates automatically.
