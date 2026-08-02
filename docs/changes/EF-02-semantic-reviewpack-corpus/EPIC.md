# Semantic ReviewPack corpus — Epic

ID: `EF-02`

## Product outcome

M2 supplies four release-only, frozen semantic ReviewPack cases. Each case has
an independent hidden oracle, immutable fixture and input locks, bounded live
calls, and a privacy-minimal Markdown decision record. It adds no runner,
JSONL ledger, service, dashboard, or DLS runtime input.

## Scope and deliverables

- `docs/evaluation-m2-cases.md` defines SR-01…SR-04, their arms, hidden-oracle
  owner, exact fixture/input/oracle locks, expected routing, per-arm maximum
  call contracts, and the locked source-blind repair proof for SR-04.
- `docs/evaluation-m2-runbook.md` defines the release-only manual procedure:
  fresh task/plugin boundary, same-day paired arm order, manifest checks,
  hard-gate stop, transport-only retries, and infrastructure-failed handling.
- `docs/evaluation-m2-decisions.md` contains exactly four privacy-minimal case
  records and one M2 decision. Before live work, records are explicitly
  `planned` or `locked-not-run`; only completed records carry a terminal arm
  outcome. Its decision is `pending-live-sample` until all four records are
  complete or a stop produces terminal `aborted`/`not-clear` with the remaining
  records unrun. It becomes actionable only for a clear M2 outcome; a completed
  non-conformant or aborted sample has no keep/improve/delete decision. It is
  distinct from the closed M1 format seed.
- Existing stdlib tests and the public validator reject missing, reordered, or
  malformed case/runbook/decision data before a release-only live run. They do
  not invoke a model.
- The four disposable synthetic Git fixtures are created and locked only by
  the runbook. An immutable private custody bundle per case holds the fixture
  recipe and hidden oracle for authorised independent replay; its recorded
  digest is verified against the public lock. The DLS maintainer retains each
  bundle for one year after the M2 decision and grants read-only replay access
  on request. Repository paths, raw source, prompts, transcripts, and session
  data never enter committed documents or DLS state.

## Non-goals

- A generic evaluator, runner, case loader, JSONL ledger, database, dashboard,
  service, new public CLI, or Harbor dependency.
- Live calls from commit/PR validation, a CI trigger, iOS observations,
  component-wide ablations, or a previous-release comparison beyond the
  declared SR-03/SR-04 reference arms.
- LLM-as-a-judge, exact finding-prose matching, raw transcripts, private paths,
  secrets, or proprietary product fixtures.
- Any release or production authorization.

## Success measures

- SR-01…SR-04 are reproducible from their locked disposable fixtures and the
  matching private custody bundle; all required lock fields are recorded before
  each live arm.
- SR-02 cannot receive `review-clear`; SR-03 proves its actual routing/call
  bounds; SR-04 permits at most one source-blind repair; no current arm has a
  hard blocker miss or safety violation. A safety violation is a hidden-oracle
  confirmed dangerous defect in a current arm that reached `review-clear`, or a
  failed current-arm safety hard oracle; it is recorded as a nonzero arm count.
  The declared SR-03 component-off miss is expected contrast evidence, not a
  current-arm clearance.
- A completed release sample has exactly seven nominal live analysis/repair
  attempts across four cases and at most eight after its one transport retry.
  An aborted sample records only its bounded partial attempts. Any incomplete,
  unintended invalid, infrastructure-failed, or budget-exhausted case is
  `not-clear`, never a PASS; the declared SR-04 fail-closed reference is
  expected contrast evidence.
- One documented keep, improve, or delete decision follows the recorded M2
  evidence without asserting release or production readiness.

## Dependencies

- `EF-01 accepted-in-base` with the accepted EF-01 definition digest and its
  acceptance SHA as an ancestor of the EF-02 candidate. DLS dependency state,
  not copied receipts or Markdown, is the implementation gate.

## Epic acceptance

- `REQ-001`: The three M2 Markdown artifacts have fixed, validated grammar for
  all four cases and contain no private data or executable DLS input.
- `REQ-002`: Every SR case records immutable fixture/input/oracle locks,
  hidden-oracle owner, expected verdict, arm manifest, hard-oracle result,
  routing, per-arm maximum-call contract, and the allowed outcome taxonomy
  before its live arm is evaluated. SR-04 additionally records a locked
  source-blind repair-access proof. SR-01 and SR-02 are current-only; SR-03 and
  SR-04 alone have reference arms.
- `REQ-003`: The release-only runbook enforces the accepted M1 dependency,
  fresh-task/plugin boundary, current/reference pairing only where a reference
  exists, at most four cases and eight counted attempts, zero live calls in
  normal validation, and a fail-closed incomplete outcome. A stop creates a
  terminal `aborted`/`not-clear` record with only its executed prefix; a
  transport retry counts toward every case/sample ceiling and cannot create a
  ninth attempt.
- `REQ-004`: Four live M2 cases execute against the locked fixtures: clean
  control, seeded blocker, critical secondary routing, and malformed-output
  repair. Their hidden oracles are evaluated outside the reviewer prompt.
- `REQ-005`: The matcher classifies useful, noisy, dangerous-miss, or uncertain
  findings without a second LLM judge. A validator-enforced M2 outcome is clear
  only when every required current/reference condition and meter passes, except
  the two declared contrast references; any current safety violation or failed
  safety hard oracle makes it `not-clear`. Only useful findings from a clear M2
  outcome can support the recorded decision.

## Risk rationale

Control level: standard. Live model evaluation can falsely clear a real defect,
leak private evidence, or consume unbounded budget. Immutable locks, hidden
oracles, strict privacy, bounded release-only execution, independent review,
and a separate human acceptance gate make these risks explicit.
