# Human boundaries

Definition, architecture, and design are separate digest-scoped approvals. They
may be written atomically from one explicit user response, but one decision
never implies another.

DLS renders pending approvals as a digest-bound `human_decision` card. Show the
listed decisions and ask `Да / Нет`; the user does not repeat SHA or digests.
Pass the hidden card ID with the verbatim affirmative response. DLS recomputes
the card before writing and rejects drift or a negative/ambiguous answer.

Standard/critical definition approval requires a current semantic definition
review. Exact-HEAD validation does not prove review. Review-clear does not prove
acceptance. Acceptance does not prove release or production.

UI work uses one of:

- exact committed precedent or artifact;
- immutable external version;
- explicit bypass with rationale.

Only an independent ReviewIR may verify findings. Implementation may mark a
finding `addressed` or `note`; a human may explicitly waive it.
