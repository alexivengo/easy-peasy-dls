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
