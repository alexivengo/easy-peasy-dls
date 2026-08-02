# DLS v0.13 CLI

Invoke the installed plugin-local launcher:

```text
python3 <plugin-root>/scripts/dls.py --root <repo> --json <command>
```

Public commands:

```text
init
doctor
new
adopt
upgrade --dry-run|--apply
status CHANGE_ID [--details findings|receipt|metrics|history]
approve CHANGE_ID --decision definition|architecture|design|accept [--decision-id ID]
ticket CHANGE_ID TICKET_ID --status STATUS
dependency set|list|remove
candidate-ready CHANGE_ID [--base REF] [--address ID] [--note ID]
review-run CHANGE_ID --kind definition|code --stream
worktree prepare CHANGE_ID [--base REF]
```

Use `--help` for exact flags. Normal users do not type these commands. The skill
does not pass operation IDs, state revisions, ReviewPack paths, evidence paths,
or arbitrary argv.

Expected boundary results use exit code 0 and a typed `next_action`. Non-zero
means usage, integrity, configuration, or infrastructure failure.

For normal human decisions, read `human_decision` from `status` or `review-run`,
show its summary and prompt, then pass its hidden `id` as `--decision-id` with
the user's verbatim `--response`. A current card accepts `Да`; DLS derives and
records the exact HEAD/digests. Do not ask the user to copy them. Legacy direct
CLI use without a decision ID keeps the explicit digest/SHA contract.

For a repeated initial candidate, omit `--base`: DLS reuses its preserved Git
base and rejects a conflicting replacement. Stream events are terminal only
when `event=completed` and `terminal=true`.

`status`, `candidate-ready`, and `review-run` return
`execution_context.contract=dls-execution-context/v1`. Resolve it before product
work:

- `ready`: run all repository operations from `owner_root`;
- `prepare-owner-worktree` or `bind-owner-worktree`: invoke `worktree prepare
  CHANGE_ID` without `--base`, then repeat status once;
- `commit-owner-source`: ask `Продолжить существующий черновик? Да / Нет.` once.
  On the immediately following `Да`, preserve the existing diff and continue in
  `owner_root`; on `Нет`, stop. Never stash, reset, overwrite, or move files.
- `resolve-owner-conflict`: stop without mutation.

`caller_root` and `owner_root` are local machine routing values. Do not repeat
them in user-facing summaries or persist them in canonical artifacts.

## Long-running review in Codex App

`functions.exec` wraps `tools.exec_command`; the two layers have different
session handles. Keep the wrapper alive until the nested command exits:

```javascript
let result = await tools.exec_command({
  cmd: "python3 <plugin-root>/scripts/dls.py --root <repo> --json review-run <change> --kind code --stream",
  workdir: "<repo>",
  yield_time_ms: 30000,
  max_output_tokens: 30000,
});
text(result.output);
while (result.session_id) {
  result = await tools.write_stdin({
    session_id: result.session_id,
    chars: "",
    yield_time_ms: 55000,
    max_output_tokens: 30000,
  });
  text(result.output);
}
```

The outer cell may itself yield and be resumed with `functions.wait`; that is
safe only while the JavaScript above still owns and polls the nested session.
Never print only `result.output` and discard `result.session_id`.
