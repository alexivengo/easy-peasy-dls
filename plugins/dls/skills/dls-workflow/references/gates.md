# Human boundaries

Definition, architecture, and design are separate digest-scoped approvals. They
may be written atomically from one explicit user response, but one decision
never implies another.

Standard/critical definition approval requires a current semantic definition
review. Exact-HEAD validation does not prove review. Review-clear does not prove
acceptance. Acceptance does not prove release or production.

UI work uses one of:

- exact committed precedent or artifact;
- immutable external version;
- explicit bypass with rationale.

Only an independent ReviewIR may verify findings. Implementation may mark a
finding `addressed` or `note`; a human may explicitly waive it.
