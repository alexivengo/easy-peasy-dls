# DLS remediation

Use this procedure in the existing implementation task after an imported `not-clear` review. Product source has one writer.

## 1. Verify canonical input before edits

Every successful actionable `not-clear` import already creates a canonical
manifest. Resolve it from the implementation checkout or any checkout with a
registered owner worktree:

```text
dls --root OWNER_ROOT --json remediation-start CHANGE_ID
```

Read only the returned manifest and its named inputs. It binds exactly the latest imported, intact, current ReviewIR, open findings, affected tickets/requirements/paths, blast-radius triggers, and current evidence. Do not load historical ReviewIR files or paste review prose.

Stop if the result is stale or tampered. For a legacy imported ReviewIR that
predates automatic manifests, perform only the typed recovery action:

```text
dls --root OWNER_ROOT --json remediation-recover CHANGE_ID \
  --review-id REVIEW_ID --operation-id <stable-id> --dry-run

dls --root OWNER_ROOT --json remediation-recover CHANGE_ID \
  --review-id REVIEW_ID --operation-id <stable-id>
```

Recovery validates the reviewed Git object and descendant relationship without
switching or resetting the checkout. Never restore an old HEAD merely to create
context.

## 2. Remediate

Fix every actionable finding in scope and inspect the manifest blast-radius triggers. A behavior, architecture, public contract, or acceptance-criteria change is an authored definition change and requires a new scoped approval. Finding status, evidence, generated regions, and DLS state do not belong in authored SPEC/TICKETS.

After source changes, run focused regression checks and commit the product candidate. Do not manually record evidence, read state revision, or call `finding set`.

For a disputed or incorrectly staged finding, use `note` with a current-SHA rationale instead of pretending to fix or waive it. This only permits independent adjudication in the next review; it does not close the finding. Never set `verified`. Only the next independent ReviewIR import can verify a finding. `resolved` is a deprecated alias for `addressed`.

## 3. Prepare the next candidate

Run one deterministic command through the plugin-local CLI:

```text
dls --root OWNER_ROOT --json candidate-ready CHANGE_ID \
  --address FIXED_FINDING_ID ... \
  --note ADJUDICATION_FINDING_ID ...
```

List every current actionable finding exactly once as `--address` or `--note`; omit human-waived findings. DLS infers the epic base, runs `policy.review_required_commands`, records exact-HEAD evidence, attaches all required evidence to each addressed finding, and atomically creates the next ReviewPack. Do not pass SHA, evidence paths, state revision, or operation ID.

Wait for this original process. Do not start another. If host execution is lost or unusually long, read only compact `candidate-status` and report at most one heartbeat per minute. On a typed blocking action, perform that action in this implementation task and retry automatically; ask the user only when the action is an explicit human boundary such as definition approval.

Handoff only the short review request. The reviewer resolves the registered worktree, ignores stale unfinished packs, and may create the current remediation pack automatically.

Stop this implementation task after `candidate-ready` returns
`open-review-task`. Never invoke `review-run` from the remediation task.
