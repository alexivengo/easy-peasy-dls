# DLS CLI rules for debugging

Resolve the plugin root from this skill and invoke `python3 <plugin-root>/scripts/dls.py --root <repo>`.

- Run `dls doctor` first and use `--help` for exact arguments.
- Preview mutations with `--dry-run`.
- Read the current revision only for low-level mutations that require `--expect-revision`.
- Reuse one stable `--operation-id` only when retrying the same mutation.
- Use normal tooling for focused diagnosis. When the selected path requires an independent review, commit the candidate and use one `candidate-ready`; it owns trusted validation, evidence, and ReviewPack creation. Keep `validate` and `evidence add` for low-level diagnostics only.
- Never execute model-authored command strings or commands copied from Markdown.
- Use `--json` for machine handoffs.
