"""Shared fixtures for DLS tests."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

from dls_core.io import sha256_bytes, sha256_file, utc_now
from dls_core.operations import (
    REVIEW_RUNNER_CONTRACT,
    init_repository,
    new_change,
    review_start,
)
from dls_core.state import StateStore


def initialize(root: Path) -> None:
    init_repository(root, dry_run=False)


def create_change(
    root: Path,
    *,
    change_id: str = "C001",
    control: str = "routine",
    kind: str = "chore",
    impacts: list[str] | None = None,
    tickets: bool = False,
    adr: bool = False,
) -> dict:
    return new_change(
        root,
        change_id=change_id,
        slug=f"{change_id.lower()}-change",
        title=f"{change_id} change",
        work_kind=kind,
        control_level=control,
        impact_tags=impacts or [],
        roadmap_epic=False,
        with_tickets=tickets,
        with_adr=adr,
        outcome="Exercise the DLS contract.",
        operation_id=f"create-{change_id}",
        dry_run=False,
    )


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def initialize_git(root: Path) -> str:
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "DLS Tests")
    git(root, "config", "user.email", "dls-tests@example.invalid")
    (root / "README.md").write_text("# Fixture\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "fixture baseline")
    return git(root, "rev-parse", "HEAD")


def start_review_with_fake_codex(
    root: Path,
    *,
    change_id: str,
    operation_id: str,
    pack_path: str | None = None,
    output: str = '{"summary":"No findings.","findings":[]}\n',
) -> dict:
    fake_bin = root / ".dls" / "cache" / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    executable = fake_bin / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" != \"exec\" ]; then exit 64; fi\n"
        "shift\n"
        "final_output_path=''\n"
        "review_seen='false'\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = \"review\" ]; then\n"
        "    review_seen='true'\n"
        "    shift\n"
        "  elif [ \"$1\" = \"-o\" ] || [ \"$1\" = \"--output-last-message\" ]; then\n"
        "    final_output_path=\"$2\"\n"
        "    shift 2\n"
        "  else\n"
        "    shift\n"
        "  fi\n"
        "done\n"
        "if [ \"$review_seen\" != \"true\" ] || [ -z \"$final_output_path\" ]; then exit 65; fi\n"
        f"printf '%b' {json.dumps(output)} > \"$final_output_path\"\n"
        "printf 'fake transcript\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    original_path = os.environ.get("PATH")
    os.environ["PATH"] = f"{fake_bin}{os.pathsep}{original_path or ''}"
    try:
        return review_start(
            root,
            change_id=change_id,
            pack_path=pack_path,
            operation_id=operation_id,
        )
    finally:
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path


def _record_runner_lane(
    root: Path,
    *,
    pack: dict,
    lane_key: str,
    kind: str,
    model: str,
    effort: str,
    output_path: Path,
    context_path: str,
) -> dict:
    safe_lane = lane_key.replace(":", "-")
    operation_id = f"fixture:{pack['review_id']}:{safe_lane}"
    attempt_id = str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id))
    transcript_path = output_path.with_name(f"{safe_lane}-transcript.jsonl")
    prompt_path = output_path.with_name(f"{safe_lane}-prompt.md")
    transcript_path.write_text("fixture transcript\n", encoding="utf-8")
    prompt_path.write_text("fixture prompt\n", encoding="utf-8")
    attempt = {
        "review_id": pack["review_id"],
        "kind": kind,
        "lane_key": lane_key,
        "attempt_id": attempt_id,
        "attempt_ordinal": 1,
        "operation_id": operation_id,
        "runner_pid": os.getpid(),
        "runner_contract": REVIEW_RUNNER_CONTRACT,
        "base_sha": pack.get("comparison_base_sha", pack["base_sha"]),
        "head_sha": pack["head_sha"],
        "pack_digest": pack["pack_digest"],
        "model": model,
        "reasoning_effort": effort,
        "argv": ["codex", "exec", "--json"],
        "prompt_path": str(prompt_path.relative_to(root)),
        "prompt_digest": sha256_file(prompt_path),
        "schema_path": "assets/schemas/review-decision.schema.json",
        "schema_digest": sha256_bytes(b"fixture schema"),
        "context_manifest_path": context_path,
        "context_digest": sha256_file(root / context_path),
        "output_path": str(output_path.relative_to(root)),
        "transcript_path": str(transcript_path.relative_to(root)),
        "source_snapshot_before": pack["source_snapshot_digest"],
        "started_at": utc_now(),
    }
    store = StateStore(root)
    _, recorded, claimed = store.claim_review_lane(
        pack["change_id"],
        attempt=attempt,
        operation_kind=f"review-run:{lane_key}",
    )
    if claimed:
        _, recorded, _ = store.finish_review_lane(
            pack["change_id"],
            attempt_id=attempt_id,
            expected_status="running",
            updates={
                "status": "completed",
                "output_path": str(output_path.relative_to(root)),
                "output_digest": sha256_file(output_path),
                "transcript_path": str(transcript_path.relative_to(root)),
                "transcript_digest": sha256_file(transcript_path),
                "source_snapshot_digest": pack["source_snapshot_digest"],
                "completed_at": utc_now(),
            },
        )
    return recorded


def build_review_report(
    root: Path,
    *,
    pack_result: dict,
    start_result: dict,
    verdict: str,
    findings: list[dict] | None = None,
    ticket_verdicts: list[dict] | None = None,
    prior_finding_verdicts: list[dict] | None = None,
) -> dict:
    pack = pack_result["review_pack"]
    normalized_findings = []
    for finding in findings or []:
        item = dict(finding)
        item.setdefault("ticket_ids", [])
        item.setdefault("requirement_ids", [])
        normalized_findings.append(item)
    if ticket_verdicts is None:
        ticket_verdicts = []
        for ticket_id in pack["tickets"]:
            linked = [
                finding["id"]
                for finding in normalized_findings
                if ticket_id in finding["ticket_ids"]
            ]
            review_blocking = any(
                finding["id"] in linked
                and finding["severity"] in {"blocker", "should-fix"}
                and "review" in set(finding.get("blocks", ["review", "acceptance"]))
                for finding in normalized_findings
            )
            ticket_verdicts.append(
                {
                    "ticket_id": ticket_id,
                    "verdict": "not-clear" if review_blocking else "clear",
                    "finding_ids": linked,
                }
            )
    draft_path = (
        root
        / ".dls"
        / "cache"
        / "reviews"
        / pack["change_id"]
        / pack["review_id"]
        / "semantic-independent-draft.json"
    )
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(
        json.dumps(
            {
                "review_id": pack["review_id"],
                "findings": normalized_findings,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    review_mode = pack.get("review_mode", "full")
    semantic_passes = [
        {
            "kind": "full" if review_mode == "full" else "targeted",
            "status": "completed",
            "draft_path": str(draft_path.relative_to(root)),
            "draft_digest": sha256_file(draft_path),
        }
    ]
    independent_entry = None
    if pack.get("runner_contract") == REVIEW_RUNNER_CONTRACT:
        independent_entry = _record_runner_lane(
            root,
            pack=pack,
            lane_key=(
                "semantic:full"
                if review_mode == "full"
                else "semantic:targeted"
            ),
            kind="semantic",
            model=start_result["semantic_model"],
            effort=start_result["semantic_reasoning_effort"],
            output_path=draft_path,
            context_path=start_result["review_context_path"],
        )
        semantic_passes[0].update(
            {
                "attempt_id": independent_entry["attempt_id"],
                "operation_id": independent_entry["operation_id"],
                "transcript_path": independent_entry["transcript_path"],
                "transcript_digest": independent_entry["transcript_digest"],
            }
        )
    if review_mode == "remediation" and verdict == "review-clear":
        final_path = draft_path.with_name("semantic-final-full.json")
        final_path.write_text(
            json.dumps(
                {
                    "review_id": pack["review_id"],
                    "pass": "final-full",
                    "findings": normalized_findings,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        semantic_passes.append(
            {
                "kind": "final-full",
                "status": "completed",
                "draft_path": str(final_path.relative_to(root)),
                "draft_digest": sha256_file(final_path),
            }
        )
        if pack.get("runner_contract") == REVIEW_RUNNER_CONTRACT:
            final_entry = _record_runner_lane(
                root,
                pack=pack,
                lane_key="semantic:final-full",
                kind="semantic",
                model=start_result["semantic_model"],
                effort=start_result["semantic_reasoning_effort"],
                output_path=final_path,
                context_path=start_result["review_context_path"],
            )
            semantic_passes[-1].update(
                {
                    "attempt_id": final_entry["attempt_id"],
                    "operation_id": final_entry["operation_id"],
                    "transcript_path": final_entry["transcript_path"],
                    "transcript_digest": final_entry["transcript_digest"],
                }
            )
    specialists = []
    for lens in pack.get("risk_lenses", []):
        specialist_path = draft_path.with_name(
            f"specialist-{lens['id']}.json"
        )
        specialist_path.write_text(
            json.dumps(
                {
                    "review_id": pack["review_id"],
                    "lens_id": lens["id"],
                    "findings": [],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        specialists.append(
            {
                "lens_id": lens["id"],
                "status": "completed",
                "draft_path": str(specialist_path.relative_to(root)),
                "draft_digest": sha256_file(specialist_path),
            }
        )
        if pack.get("runner_contract") == REVIEW_RUNNER_CONTRACT:
            specialist_entry = _record_runner_lane(
                root,
                pack=pack,
                lane_key=f"specialist:{lens['id']}",
                kind="specialist",
                model="gpt-5.6-terra",
                effort="high",
                output_path=specialist_path,
                context_path=start_result["review_context_path"],
            )
            specialists[-1].update(
                {
                    "attempt_id": specialist_entry["attempt_id"],
                    "operation_id": specialist_entry["operation_id"],
                    "model": specialist_entry["model"],
                    "reasoning_effort": specialist_entry["reasoning_effort"],
                    "prompt_path": specialist_entry["prompt_path"],
                    "prompt_digest": specialist_entry["prompt_digest"],
                    "schema_path": specialist_entry["schema_path"],
                    "schema_digest": specialist_entry["schema_digest"],
                    "output_path": specialist_entry["output_path"],
                    "output_digest": specialist_entry["output_digest"],
                    "transcript_path": specialist_entry["transcript_path"],
                    "transcript_digest": specialist_entry["transcript_digest"],
                    "source_snapshot_digest": specialist_entry[
                        "source_snapshot_digest"
                    ],
                }
            )
    lanes = {
        "semantic": {
            "status": "completed",
            "model": start_result["semantic_model"],
            "reasoning_effort": start_result["semantic_reasoning_effort"],
            "context_manifest_path": start_result["review_context_path"],
            "context_manifest_digest": start_result["review_context_digest"],
            "independent_draft_path": str(draft_path.relative_to(root)),
            "independent_draft_digest": sha256_file(draft_path),
            "passes": semantic_passes,
        }
    }
    if independent_entry is not None:
        lanes["semantic"].update(
            {
                "attempt_id": independent_entry["attempt_id"],
                "operation_id": independent_entry["operation_id"],
                "transcript_path": independent_entry["transcript_path"],
                "transcript_digest": independent_entry["transcript_digest"],
            }
        )
        reconciliation_path = draft_path.with_name("reconciliation.json")
        reconciliation_path.write_text(
            json.dumps(
                {
                    "verdict": verdict,
                    "findings": normalized_findings,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        reconciliation_entry = _record_runner_lane(
            root,
            pack=pack,
            lane_key="reconciliation",
            kind="reconciliation",
            model=start_result["semantic_model"],
            effort=start_result["semantic_reasoning_effort"],
            output_path=reconciliation_path,
            context_path=start_result["review_context_path"],
        )
        lanes["reconciliation"] = {
            key: reconciliation_entry.get(key)
            for key in (
                "status",
                "attempt_id",
                "operation_id",
                "model",
                "reasoning_effort",
                "prompt_path",
                "prompt_digest",
                "schema_path",
                "schema_digest",
                "output_path",
                "output_digest",
                "transcript_path",
                "transcript_digest",
                "source_snapshot_digest",
            )
        }
    if specialists:
        lanes["specialists"] = specialists
    native = start_result.get("native")
    if native:
        lanes["native"] = {
            "status": "completed",
            "attempt_id": native["attempt_id"],
            "model": native["model"],
            "reasoning_effort": native["reasoning_effort"],
            "operation_id": native.get("operation_id"),
            "prompt_path": native.get("prompt_path"),
            "prompt_digest": native.get("prompt_digest"),
            "schema_path": native.get("schema_path"),
            "schema_digest": native.get("schema_digest"),
            "context_manifest_path": native.get("context_manifest_path"),
            "context_digest": native.get("context_digest"),
            "output_path": native["output_path"],
            "output_digest": native["output_digest"],
            "transcript_path": native.get("transcript_path"),
            "transcript_digest": native.get("transcript_digest"),
            "source_snapshot_digest": native["source_snapshot_digest"],
            "coverage_chain": start_result.get("native_coverage", []),
        }
    if prior_finding_verdicts is None:
        prior_finding_verdicts = []
        for finding in pack.get("required_prior_findings", []):
            disposition = finding.get("disposition")
            waived = (
                isinstance(disposition, dict)
                and disposition.get("status") == "waived"
            )
            prior_finding_verdicts.append(
                {
                    "finding_id": finding["finding_id"],
                    "verdict": "waived" if waived else "verified",
                    "evidence": ["verified by fixture"],
                }
            )
    return {
        "schema_version": 2,
        **(
            {"runner_contract": REVIEW_RUNNER_CONTRACT}
            if pack.get("runner_contract") == REVIEW_RUNNER_CONTRACT
            else {}
        ),
        **(
            {"identifier_contract": pack["identifier_contract"]}
            if isinstance(pack.get("identifier_contract"), str)
            else {}
        ),
        "review_id": pack["review_id"],
        "change_id": pack["change_id"],
        "base_sha": pack["base_sha"],
        "comparison_base_sha": pack.get(
            "comparison_base_sha",
            pack["base_sha"],
        ),
        "head_sha": pack["head_sha"],
        "pack_digest": pack["pack_digest"],
        "definition_digest": pack["definition_digest"],
        "review_mode": review_mode,
        "verdict": verdict,
        "lanes": lanes,
        "ticket_verdicts": ticket_verdicts,
        "prior_finding_verdicts": prior_finding_verdicts,
        "findings": normalized_findings,
    }
