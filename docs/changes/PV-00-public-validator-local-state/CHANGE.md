# Ignore local DLS state in public validator

ID: `PV-00`
Kind: `bug`

## Outcome

The public validator passes in a DLS owner worktree while continuing to reject tracked or working-tree forbidden artifacts.

## Scope

- Exclude the untracked local `.dls` directory from the public validator's
  working-tree discovery.
- Keep `git ls-files` unchanged, so a tracked `.dls` artifact remains
  forbidden.

## Non-goals

- Changes to EF-00 definition artifacts or other validator policy.
- Ignoring any other untracked files.

## Requirements and acceptance

- `REQ-001`: `scripts/validate_public_repo.py` ignores local untracked `.dls`
  state created by DLS, while a tracked `.dls` path remains subject to the
  existing forbidden-artifact check.
- Acceptance: the public validator passes in the DLS owner worktree; the
  tracked-file source remains unchanged and `FORBIDDEN_PATH_PARTS` still
  contains `.dls`.

## Technical approach

Filter `.dls` only in `working_tree_files()`. Filtering the `git ls-files`
result would weaken protection against accidentally tracked state.

## Validation intent

- `python3 scripts/validate_public_repo.py`
- `python3 plugins/dls/scripts/run_tests.py`
- `python3 -m compileall -q plugins/dls/scripts plugins/dls/hooks`

## Risk rationale

Control level: routine.

## UI/UX source

<!-- dls:design:start -->
Not applicable unless the change affects user-interface surfaces.
<!-- dls:design:end -->
