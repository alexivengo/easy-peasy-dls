#!/usr/bin/env python3
"""Deterministic Codex stand-in for v0.4.3 recovery smoke tests."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def record(kind: str) -> None:
    value = os.environ.get("DLS_FAKE_CALL_LOG")
    if not value:
        return
    path = Path(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(kind + "\n")


args = sys.argv[1:]
if "review" in args:
    record("native-unexpected")
    raise SystemExit(90)

output = Path(args[args.index("--output-last-message") + 1])
prompt = Path(".dls-review-input/prompt.md").read_text(encoding="utf-8")

if prompt.startswith("# DLS decision-reference repair"):
    kind = "decision-repair"
    forbidden = (
        Path(".dls-review-input/context.json"),
        Path(".dls-review-input/native.txt"),
        Path("README.md"),
        Path("Sources"),
    )
    if any(path.exists() for path in forbidden):
        raise SystemExit(91)
    bundle = json.loads(
        Path(".dls-review-input/repair.json").read_text(encoding="utf-8")
    )
    payload = bundle["raw_decision"]
    allowed = bundle["allowed_ticket_ids"]
    for finding in payload.get("findings", []):
        finding["ticket_ids"] = [
            value for value in finding.get("ticket_ids", []) if value in allowed
        ]
    for prior in payload.get("prior_finding_verdicts", []):
        prior_id = prior["finding_id"]
        replacement_id = bundle["reserved_replacement_ids"].get(prior_id)
        if replacement_id is None:
            continue
        source = bundle["canonical_prior_findings"][prior_id]
        prior["replacement_finding_id"] = replacement_id
        payload["findings"].append(
            {
                "id": replacement_id,
                "severity": source["severity"],
                "kind": source["kind"],
                "location": source["location"],
                "issue": source["issue"],
                "impact": source["impact"],
                "required_fix": source["required_fix"],
                "ticket_ids": source["ticket_ids"],
                "requirement_ids": source["requirement_ids"],
                "blocks": source["blocks"],
                "provenance": ["v0.4.3 disposable repair fixture"],
            }
        )
elif prompt.startswith("# DLS review reconciliation"):
    kind = "reconciliation"
    payload = json.loads(
        Path(".dls-review-input/semantic-independent.json").read_text(
            encoding="utf-8"
        )
    )
elif prompt.startswith("# DLS remediation final-full review"):
    kind = "final-full"
    payload = json.loads(
        Path(".dls-review-input/targeted-decision.json").read_text(
            encoding="utf-8"
        )
    )
else:
    record("unexpected-semantic-lane")
    raise SystemExit(92)

output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(payload), encoding="utf-8")
record(kind)
print(json.dumps({"type": "fake", "lane": kind}))
