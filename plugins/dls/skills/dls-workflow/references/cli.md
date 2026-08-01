# DLS v0.11 CLI

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
approve CHANGE_ID --decision definition|architecture|design|accept
ticket CHANGE_ID TICKET_ID --status STATUS
dependency set|list|remove
candidate-ready CHANGE_ID [--base REF] [--address ID] [--note ID]
review-run CHANGE_ID --kind definition|code --stream
worktree prepare CHANGE_ID --base REF
```

Use `--help` for exact flags. Normal users do not type these commands. The skill
does not pass operation IDs, state revisions, ReviewPack paths, evidence paths,
or arbitrary argv.

Expected boundary results use exit code 0 and a typed `next_action`. Non-zero
means usage, integrity, configuration, or infrastructure failure.

For a repeated initial candidate, omit `--base`: DLS reuses its preserved Git
base and rejects a conflicting replacement. Stream events are terminal only
when `event=completed` and `terminal=true`.

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
