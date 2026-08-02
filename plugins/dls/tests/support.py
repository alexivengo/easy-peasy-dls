"""Small fixtures for the v0.11 current-only contract."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from dls_core.core import create_change, init_repository


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def repository(root: Path) -> str:
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "DLS Tests")
    git(root, "config", "user.email", "dls@example.invalid")
    (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "baseline")
    init_repository(root, dry_run=False)
    return git(root, "rev-parse", "HEAD")


def configure(root: Path, *, command: str = "pass") -> None:
    body = (
        'schema_version = 1\n'
        'docs_root = "docs/changes"\n'
        'default_profile = "generic"\n\n'
        '[policy]\n'
        'review_required_commands = ["test"]\n'
        'acceptance_required_commands = ["test"]\n\n'
        '[commands.test]\n'
        f'argv = ["python3", "-c", {json.dumps(command)}]\n'
        'cwd = "."\n'
        'timeout_seconds = 10\n'
        'max_output_bytes = 8192\n'
        'env_allow = []\n'
    )
    (root / ".dls" / "config.toml").write_text(body, encoding="utf-8")


def change(
    root: Path,
    *,
    change_id: str = "C001",
    control: str = "routine",
    impacts: list[str] | None = None,
    tickets: bool = True,
    adr: bool = False,
) -> dict:
    return create_change(
        root,
        change_id=change_id,
        slug=change_id.lower(),
        title=f"{change_id} change",
        kind="feature",
        control=control,
        impact_tags=impacts or [],
        outcome="Deliver a tested change.",
        with_tickets=tickets,
        with_adr=adr,
        dry_run=False,
    )


def commit(root: Path, message: str) -> str:
    git(root, "add", ".")
    git(root, "commit", "-m", message)
    return git(root, "rev-parse", "HEAD")


def fake_codex(root: Path, source: str) -> tuple[Path, dict[str, str | None]]:
    bin_dir = root / ".dls" / "cache" / "fake-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    executable = bin_dir / "codex"
    executable.write_text(source, encoding="utf-8")
    executable.chmod(0o755)
    previous = os.environ.get("PATH")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{previous or ''}"
    return executable, {"PATH": previous}


def restore_environment(previous: dict[str, str | None]) -> None:
    value = previous["PATH"]
    if value is None:
        os.environ.pop("PATH", None)
    else:
        os.environ["PATH"] = value


FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]
out=pathlib.Path(args[args.index("--output-last-message")+1])
prompt=sys.stdin.read()
payload=json.loads(prompt.split("INPUT:\n",1)[1]) if "INPUT:\n" in prompt else {}
log=pathlib.Path(__file__).with_name("calls.jsonl")
with log.open("a") as f: f.write(json.dumps({"model":args[args.index("--model")+1],"prompt":prompt[:80]})+"\n")
findings=[]
mode_path=pathlib.Path(__file__).with_name("mode")
mode=mode_path.read_text().strip() if mode_path.exists() else "clear"
if mode=="finding":
    ticket=next(iter(payload.get("tickets",{})),"")
    requirement=next(iter(payload.get("requirement_ids",[])),"")
    findings=[{
      "id":"NEW-1","severity":"should-fix","kind":"defect","location":"src.py:1",
      "issue":"bug","impact":"wrong result","required_fix":"fix it",
      "ticket_ids":[ticket] if ticket else [],
      "requirement_ids":[requirement] if requirement else [],
      "blocks":["review","acceptance"],"provenance":["exact diff"]
    }]
def rows(ids,key):
    return [{key:item,"verdict":"not-clear" if findings else "clear","finding_ids":["NEW-1"] if findings else []} for item in ids]
prior=[]
for item in payload.get("prior_findings",[]):
    prior.append({"finding_id":item["id"],"verdict":"verified","replacement_finding_id":None,"evidence":["exact diff"]})
decision={
 "verdict":"not-clear" if findings else "review-clear",
 "summary":"finding" if findings else "clear",
 "findings":findings,
 "ticket_verdicts":rows(payload.get("tickets",{}),"ticket_id"),
 "requirement_verdicts":rows(payload.get("requirement_ids",[]),"requirement_id"),
 "prior_finding_verdicts":prior
}
out.write_text(json.dumps(decision))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":20,"output_tokens":50}}))
'''

FAKE_REPAIR = r'''#!/usr/bin/env python3
import json, pathlib, sys
args=sys.argv[1:]
out=pathlib.Path(args[args.index("--output-last-message")+1])
prompt=sys.stdin.read()
log=pathlib.Path(__file__).with_name("calls.jsonl")
with log.open("a") as f: f.write(json.dumps({"repair":prompt.startswith("Repair only"),"prompt":prompt[:120]})+"\n")
if prompt.startswith("Repair only"):
    bundle=json.loads(prompt.split("\n",1)[1])
    tickets=bundle["ticket_ids"]; requirements=bundle["requirement_ids"]; prior=bundle["prior_findings"]
    prior_rows=[{"finding_id":item["id"],"verdict":"verified","replacement_finding_id":None,"evidence":["exact diff"]} for item in prior]
else:
    payload=json.loads(prompt.split("INPUT:\n",1)[1])
    tickets=list(payload["tickets"]); requirements=payload["requirement_ids"]; prior=payload["prior_findings"]
    prior_rows=[{"finding_id":item["id"],"verdict":"still-open","replacement_finding_id":None,"evidence":[]} for item in prior]
decision={
 "verdict":"review-clear","summary":"clear","findings":[],
 "ticket_verdicts":[{"ticket_id":item,"verdict":"clear","finding_ids":[]} for item in tickets],
 "requirement_verdicts":[{"requirement_id":item,"verdict":"clear","finding_ids":[]} for item in requirements],
 "prior_finding_verdicts":prior_rows
}
out.write_text(json.dumps(decision))
print(json.dumps({"usage":{"input_tokens":100,"output_tokens":50}}))
'''

FAKE_TICKET_LIFECYCLE_REPAIR = r'''#!/usr/bin/env python3
import json, pathlib, sys
args=sys.argv[1:]
out=pathlib.Path(args[args.index("--output-last-message")+1])
prompt=sys.stdin.read(); repair=prompt.startswith("Repair only")
log=pathlib.Path(__file__).with_name("calls.jsonl")
with log.open("a") as f: f.write(json.dumps({"repair":repair,"prompt":prompt[:400]})+"\n")
if repair:
    bundle=json.loads(prompt.split("\n",1)[1]); tickets=bundle["ticket_ids"]
    requirements=bundle["requirement_ids"]
else:
    payload=json.loads(prompt.split("INPUT:\n",1)[1]); tickets=list(payload["tickets"])
    requirements=payload["requirement_ids"]
decision={
 "verdict":"review-clear","summary":"clear","findings":[],
 "ticket_verdicts":[{"ticket_id":item,"verdict":"clear" if repair else "blocked","finding_ids":[]} for item in tickets],
 "requirement_verdicts":[{"requirement_id":item,"verdict":"clear","finding_ids":[]} for item in requirements],
 "prior_finding_verdicts":[]
}
out.write_text(json.dumps(decision))
print(json.dumps({"usage":{"input_tokens":100,"output_tokens":50}}))
'''

FAKE_TICKET_LIFECYCLE_REPAIR_FAIL = FAKE_TICKET_LIFECYCLE_REPAIR.replace(
    '"clear" if repair else "blocked"',
    '"blocked"',
)

FAKE_ACTIONABLE_LIFECYCLE_REPAIR = r'''#!/usr/bin/env python3
import json, pathlib, sys
args=sys.argv[1:]
out=pathlib.Path(args[args.index("--output-last-message")+1])
prompt=sys.stdin.read(); repair=prompt.startswith("Repair only")
log=pathlib.Path(__file__).with_name("calls.jsonl")
with log.open("a") as f: f.write(json.dumps({"repair":repair,"prompt":prompt[:1200]})+"\n")
if repair:
    bundle=json.loads(prompt.split("\n",1)[1]); tickets=bundle["ticket_ids"]
    requirements=bundle["requirement_ids"]
else:
    payload=json.loads(prompt.split("INPUT:\n",1)[1]); tickets=list(payload["tickets"])
    requirements=payload["requirement_ids"]
finding={
 "id":"NEW-1","severity":"should-fix","kind":"governance","location":"docs/spec.md:1",
 "issue":"release evidence conflicts with cleanliness","impact":"release proof cannot complete",
 "required_fix":"separate generated evidence from the clean source tree","ticket_ids":[tickets[-1]],
 "requirement_ids":[],"blocks":["acceptance","release"] + (["review"] if repair else []),
 "provenance":["definition contract"]
}
decision={
 "verdict":"not-clear","summary":"release evidence boundary is inconsistent","findings":[finding],
 "ticket_verdicts":[
   {"ticket_id":item,"verdict":("not-clear" if item == tickets[-1] else ("clear" if repair else "blocked")),
    "finding_ids":["NEW-1"] if item == tickets[-1] else []}
   for item in tickets
 ],
 "requirement_verdicts":[{"requirement_id":item,"verdict":"clear","finding_ids":[]} for item in requirements],
 "prior_finding_verdicts":[]
}
out.write_text(json.dumps(decision))
print(json.dumps({"usage":{"input_tokens":100,"output_tokens":50}}))
'''

FAKE_CONFLICT = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args=sys.argv[1:]; model=args[args.index("--model")+1]
out=pathlib.Path(args[args.index("--output-last-message")+1]); prompt=sys.stdin.read()
log=pathlib.Path(__file__).with_name("calls.jsonl")
reconcile=prompt.startswith("Resolve only")
if reconcile:
    bundle=json.loads(prompt.split("\n",1)[1]); payload=bundle["pack"]; finding=True
else:
    payload=json.loads(prompt.split("INPUT:\n",1)[1]); finding=model.endswith("-sol")
ticket=next(iter(payload.get("tickets",{})),""); requirement=next(iter(payload.get("requirement_ids",[])),"")
findings=[{"id":"NEW-1","severity":"should-fix","kind":"defect","location":"src.py:1","issue":"bug","impact":"wrong","required_fix":"fix","ticket_ids":[ticket] if ticket else [],"requirement_ids":[requirement] if requirement else [],"blocks":["review"],"provenance":["diff"]}] if finding else []
decision={"verdict":"not-clear" if finding else "review-clear","summary":"decision","findings":findings,
"ticket_verdicts":[{"ticket_id":x,"verdict":"not-clear" if finding else "clear","finding_ids":["NEW-1"] if finding else []} for x in payload.get("tickets",{})],
"requirement_verdicts":[{"requirement_id":x,"verdict":"not-clear" if finding else "clear","finding_ids":["NEW-1"] if finding else []} for x in payload.get("requirement_ids",[])],
"prior_finding_verdicts":[]}
out.write_text(json.dumps(decision))
with log.open("a") as f: f.write(json.dumps({"model":model,"reconcile":reconcile,"source_visible":pathlib.Path("src.py").exists()})+"\n")
print(json.dumps({"usage":{"input_tokens":100,"output_tokens":50}}))
'''

FAKE_BUDGET = FAKE_CODEX.replace(
    '"input_tokens":100,"cached_input_tokens":20,"output_tokens":50',
    '"input_tokens":700000,"cached_input_tokens":20,"output_tokens":1',
)
