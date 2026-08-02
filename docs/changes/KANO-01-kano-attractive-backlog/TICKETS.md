# KANO Attractive backlog — Implementation Tickets

Contract: `SPEC.md`

## T01 — Publish the accepted Attractive backlog in Roadmap

Requirements:

- `REQ-001` through `REQ-005`

Scope:

- Add the `A11–A60` candidate catalogue from `SPEC.md` to
  `docs/roadmap.md`.
- Preserve the current active Roadmap and `Not doing` sections.
- Do not modify the archived KANO snapshot or runtime source.

Acceptance:

- The Roadmap contains all and only the 50 defined candidate IDs once.
- Every item has its `Было` and `Будет` behavior.
- The new section states its non-active and opt-in status.

Validation:

- Run the repository-owned public validator.
- Check the 50-ID sequence and duplicate-free count.
