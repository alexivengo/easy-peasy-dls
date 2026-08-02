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
- The new section contains all three promotion rules: P1 real-pilot validation
  with no more than three concurrent experiments, P2 promotion after three to
  five real pilots, and P3 hold until a repeated signal or scale need.
- The new section says candidates are opt-in rather than mandatory gates and
  that they do not alter runtime, the frozen snapshot, human decisions,
  exact-HEAD evidence, review boundaries, task creation, or global config.
- The new section says the catalogue is not an implementation commitment and
  does not claim any candidate implemented, review-clear, accepted, released,
  or production-verified.

Validation:

- Run the repository-owned public validator.
- Check the 50-ID sequence and duplicate-free count.
- Check the three promotion rules, the P1 concurrency cap, and each required
  boundary statement directly in `docs/roadmap.md`.
