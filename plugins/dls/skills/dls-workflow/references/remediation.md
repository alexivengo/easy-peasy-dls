# DLS remediation

Use this procedure in the existing implementation task after an imported `not-clear` review. Product source has one writer.

## 1. Freeze canonical input before edits

Run from the implementation checkout while HEAD still equals the latest reviewed HEAD:

```text
dls --root OWNER_ROOT --json remediation-start CHANGE_ID
```

Read only the returned manifest and its named inputs. It binds exactly the latest imported, intact, current ReviewIR, open findings, affected tickets/requirements/paths, blast-radius triggers, and current evidence. Do not load historical ReviewIR files or paste review prose.

Stop if the result is missing, stale, or tampered. If source already advanced and the manifest is missing, restore a clean checkout at the reviewed HEAD to generate it before continuing; do not synthesize an unbound manifest.

## 2. Remediate

Fix every actionable finding in scope and inspect the manifest blast-radius triggers. A behavior, architecture, public contract, or acceptance-criteria change is an authored definition change and requires a new scoped approval. Finding status, evidence, generated regions, and DLS state do not belong in authored SPEC/TICKETS.

After source changes:

1. run focused regression checks and the required full validation;
2. commit the product candidate;
3. record current evidence for that HEAD;
4. set each fixed finding to `addressed` with candidate SHA and evidence.

Never set `verified`. Only the next independent ReviewIR import can verify a finding. `resolved` is a deprecated alias for `addressed`.

## 3. Create the next candidate

Read current state revision and run:

```text
dls --root OWNER_ROOT --json review-ready CHANGE_ID --base BASE \
  --expect-revision REVISION --operation-id <stable-id> --dry-run

dls --root OWNER_ROOT --json review-ready CHANGE_ID --base BASE \
  --expect-revision REVISION --operation-id <stable-id>
```

When blocked, perform exactly the returned typed `next_action`, refresh the state revision, and retry. Do not fall back to raw `review-pack` for repeat review.

Handoff only the short review request. The reviewer resolves the registered worktree and newest unfinished pack.
