"""Current-only candidate and exact-HEAD review pipelines."""

from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .core import (
    PACK_SCHEMA,
    RESULT_SCHEMA,
    current_approval,
    current_candidate,
    current_review,
    decision_projection,
    definition_digest,
    definition_review_current,
    dependency_status,
    human_decision,
    load_state,
    mutate_state,
    next_action,
    receipt,
    stable_digest,
)
from .errors import ConfigError, IntegrityError, UsageError
from .io import atomic_write_json, read_json, safe_resolve, sha256_bytes, sha256_file, utc_now
from .repo import (
    SCHEMAS_ROOT,
    allowed_environment,
    command_config,
    command_contract_digest,
    git_head,
    git_product_tree_digest,
    git_source_dirty_paths,
    load_config,
    resolve_profile,
    run_git,
)

RUNNER_CONTRACT = "dls-review-runner/v4"
MODEL_EXECUTION_CONTRACT = "dls-model-exec/v2"
REPAIR_CONTRACT = "dls-decision-repair/v3"
ROUTING_CONTRACT = "dls-review-routing/v1"
PACK_CONTRACT = "dls-review-pack/v3"
RESULT_CONTRACT = "dls-review-ir/v3"
MODEL_PRIMARY = "gpt-5.6-terra"
MODEL_SECONDARY = "gpt-5.6-sol"
SEVERITIES = {"blocker", "should-fix", "note"}
FINDING_KINDS = {"defect", "validation-gap", "governance", "external", "design"}
BLOCKS = {"review", "acceptance", "release", "production"}
REVIEW_VERDICTS = {"review-clear", "not-clear", "blocked"}
PRIOR_VERDICTS = {"verified", "still-open", "regressed", "waived"}
ITEM_VERDICTS = {"clear", "not-clear", "blocked"}
RISK_LENSES = (
    ({"auth", "security-privacy"}, "trust", "xhigh"),
    ({"data-loss", "data-migration", "migration"}, "data", "xhigh"),
    ({"concurrency", "availability"}, "reliability", "xhigh"),
    ({"public-api", "compatibility"}, "contract", "high"),
)
BUDGETS = {
    "routine": {"primary": 650_000, "repair": 100_000, "aggregate": 750_000},
    "standard": {"primary": 1_250_000, "repair": 250_000, "aggregate": 1_500_000},
    "critical": {
        "primary": 1_250_000,
        "secondary": 1_000_000,
        "reconciliation": 500_000,
        "repair": 250_000,
        "aggregate": 3_000_000,
    },
}
MODEL_TIMEOUT_SECONDS = 20 * 60
MODEL_TRANSCRIPT_BYTES = 1024 * 1024


def _alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _run_bounded(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
    stdin: bytes | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=True,
            bufsize=0,
        )
    except OSError as exc:
        raw = str(exc).encode()
        return {
            "exit_code": 127,
            "timed_out": False,
            "overflow": False,
            "output": raw,
            "output_bytes": len(raw),
            "output_digest": sha256_bytes(raw),
            "duration_seconds": time.monotonic() - started,
        }
    assert process.stdout is not None
    if process.stdin is not None:
        try:
            process.stdin.write(stdin or b"")
            process.stdin.close()
        except BrokenPipeError:
            pass
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    retained = bytearray()
    total = 0
    digest = hashlib.sha256()
    timed_out = False
    overflow = False

    def stop() -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            process.kill()

    try:
        while selector.get_map():
            if time.monotonic() - started >= timeout_seconds:
                timed_out = True
                stop()
            events = selector.select(0.1)
            if not events and process.poll() is not None:
                events = selector.select(0)
                if not events:
                    break
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                digest.update(chunk)
                remaining = max_output_bytes - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
                if total > max_output_bytes:
                    overflow = True
                    stop()
        process.wait()
    finally:
        selector.close()
        process.stdout.close()
        stop()
    code = process.returncode if process.returncode is not None else 1
    if timed_out:
        code = 124
    elif overflow:
        code = 125
    return {
        "exit_code": code,
        "timed_out": timed_out,
        "overflow": overflow,
        "output": bytes(retained),
        "output_bytes": total,
        "output_digest": digest.hexdigest(),
        "duration_seconds": time.monotonic() - started,
    }


def _failure_path(root: Path, change_id: str) -> Path:
    return root / ".dls" / "cache" / "failure" / f"{change_id}.json"


def _record_failure(root: Path, change_id: str, payload: dict[str, Any]) -> None:
    safe = {
        key: value
        for key, value in payload.items()
        if key not in {"prompt", "decision", "transcript", "raw_output"}
    }
    atomic_write_json(_failure_path(root, change_id), safe, backup=False)


def _clear_failure(root: Path, change_id: str) -> None:
    path = _failure_path(root, change_id)
    if path.is_file():
        path.unlink()


def _claim_run(
    root: Path,
    state: dict[str, Any],
    *,
    run_id: str,
    kind: str,
    head_sha: str,
    contract_digest: str,
) -> tuple[dict[str, Any], bool]:
    active = state.get("active_run")
    if isinstance(active, dict) and active.get("run_id") == run_id:
        if active.get("status") == "running" and _alive(active.get("pid")):
            return active, False
        if active.get("status") == "completed":
            return active, False

    def apply(value: dict[str, Any]) -> None:
        previous = value.get("active_run")
        lanes = (
            previous.get("lanes", {})
            if isinstance(previous, dict) and previous.get("run_id") == run_id
            else {}
        )
        value["active_run"] = {
            "run_id": run_id,
            "kind": kind,
            "head_sha": head_sha,
            "contract_digest": contract_digest,
            "status": "running",
            "pid": os.getpid(),
            "started_at": (
                previous.get("started_at")
                if isinstance(previous, dict) and previous.get("run_id") == run_id
                else utc_now()
            ),
            "lanes": lanes,
        }

    updated = mutate_state(root, state["change"]["id"], apply)
    return updated["active_run"], True


def _finish_run(
    root: Path,
    change_id: str,
    run_id: str,
    *,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    def apply(value: dict[str, Any]) -> None:
        active = value.get("active_run")
        if not isinstance(active, dict) or active.get("run_id") != run_id:
            raise IntegrityError("Active run changed during execution")
        active["status"] = status
        active["completed_at"] = utc_now()
        active["pid"] = None
        if error:
            active["error"] = error[:2000]

    return mutate_state(root, change_id, apply)


def _set_lane(
    root: Path,
    change_id: str,
    run_id: str,
    lane: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    def apply(value: dict[str, Any]) -> None:
        active = value.get("active_run")
        if not isinstance(active, dict) or active.get("run_id") != run_id:
            raise IntegrityError("Active run changed during lane execution")
        active.setdefault("lanes", {})[lane] = payload

    return mutate_state(root, change_id, apply)


def _validation_policy(root: Path) -> tuple[list[str], str]:
    config = load_config(root)
    commands = list(config.get("policy", {}).get("review_required_commands", []))
    if not commands:
        raise ConfigError("policy.review_required_commands must list trusted command IDs")
    contracts = {item: command_contract_digest(root, item) for item in commands}
    return commands, stable_digest(contracts)


def _run_validation(root: Path, command_id: str) -> dict[str, Any]:
    command = command_config(root, command_id)
    result = _run_bounded(
        list(command["argv"]),
        cwd=safe_resolve(root, command["cwd"], must_exist=True),
        environment=allowed_environment(command["env_allow"]),
        timeout_seconds=command["timeout_seconds"],
        max_output_bytes=command["max_output_bytes"],
    )
    return {
        "command_id": command_id,
        "command_digest": command_contract_digest(root, command_id),
        "status": "pass" if result["exit_code"] == 0 else "fail",
        "exit_code": result["exit_code"],
        "duration_seconds": round(result["duration_seconds"], 3),
        "output_bytes": result["output_bytes"],
        "output_digest": result["output_digest"],
        "diagnostic": result["output"].decode("utf-8", errors="replace")[-4096:]
        if result["exit_code"]
        else None,
    }


def _require_decisions(root: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    if not definition_review_current(root, state):
        return {"id": "run-definition-review"}
    projection = decision_projection(root, state)
    missing = [
        {"decision": name, "digest": item["digest"]}
        for name, item in projection.items()
        if item["required"] and current_approval(state, name, item["digest"]) is None
    ]
    if not missing:
        return None
    return {
        "id": "approve-" + "-and-".join(item["decision"] for item in missing),
        "approvals": missing,
    }


def _requirements(root: Path, state: dict[str, Any]) -> list[str]:
    prefixes = tuple(state["change"].get("requirement_prefixes", []))
    if not prefixes:
        return []
    found: set[str] = set()
    pattern = re.compile(r"\b([A-Z][A-Z0-9]{0,15}-\d{2,})\b")
    for item in state["change"]["artifacts"].values():
        path = safe_resolve(root, item["path"], must_exist=True)
        ticket_scope = item.get("producer_ticket_scope")
        if isinstance(ticket_scope, list) and ticket_scope:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                epics = payload["epics"]
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise IntegrityError("Scoped traceability artifact must be a valid matrix") from exc
            for epic in epics.values():
                for requirement_id, record in epic.get("requirements", {}).items():
                    if (
                        isinstance(record, dict)
                        and record.get("producerTicket") in ticket_scope
                        and requirement_id.split("-", 1)[0] in prefixes
                    ):
                        found.add(requirement_id)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in pattern.finditer(text):
            value = match.group(1)
            if value.split("-", 1)[0] in prefixes:
                found.add(value)
    return sorted(found)[:256]


def _artifact_projection(root: Path, state: dict[str, Any]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for name, item in sorted(state["change"]["artifacts"].items()):
        path = safe_resolve(root, item["path"], must_exist=True)
        output.append({"name": name, "path": item["path"], "digest": sha256_file(path)})
    return output


def _pack_core(
    root: Path,
    state: dict[str, Any],
    *,
    kind: str,
    base_sha: str,
    evidence: list[dict[str, Any]],
    source_digest: str,
) -> dict[str, Any]:
    head = git_head(root)
    if not head:
        raise IntegrityError("Review requires a Git HEAD")
    projection = decision_projection(root, state)
    profile = resolve_profile(root)
    config = load_config(root)
    validation_policy_digest = (
        stable_digest(
            {
                command: command_contract_digest(root, command)
                for command in config.get("policy", {}).get("review_required_commands", [])
            }
        )
        if kind == "code"
        else None
    )
    current_findings = [
        item
        for item in state["findings"].values()
        if set(item.get("blocks", [])) & {"review", "acceptance"}
        and item.get("status") != "waived"
    ]
    return {
        "schema_version": PACK_SCHEMA,
        "contract": PACK_CONTRACT,
        "runner_contract": RUNNER_CONTRACT,
        "kind": kind,
        "change_id": state["change"]["id"],
        "control_level": state["change"]["control"],
        "impact_tags": state["change"]["impact_tags"],
        "base_sha": base_sha,
        "head_sha": head,
        "source_digest": source_digest,
        "definition_digest": projection["definition"]["digest"],
        "decision_digests": {
            key: item["digest"]
            for key, item in projection.items()
            if item["required"] and key != "definition"
        },
        "platform_profile": profile,
        "validation_policy_digest": validation_policy_digest,
        "artifacts": _artifact_projection(root, state),
        "tickets": {
            ticket_id: {"status": item["status"]}
            for ticket_id, item in sorted(state["tickets"].items())
        },
        "requirement_ids": _requirements(root, state),
        "evidence": evidence,
        "prior_review": state.get("review"),
        "prior_findings": current_findings,
    }


def _write_pack(
    root: Path,
    core: dict[str, Any],
    *,
    write: bool = True,
) -> tuple[dict[str, Any], str]:
    review_id = str(uuid.uuid5(uuid.NAMESPACE_URL, stable_digest(core)))
    pack = {**core, "review_id": review_id}
    pack["pack_digest"] = stable_digest(pack)
    relative = f".dls/reviews/{pack['change_id']}/packs/{review_id}.json"
    path = safe_resolve(root, relative)
    if path.is_file():
        if read_json(path) != pack:
            raise IntegrityError("ReviewPack collision")
    elif write:
        atomic_write_json(path, pack, backup=False)
    return pack, relative


def _candidate_base(
    root: Path,
    state: dict[str, Any],
    *,
    head: str,
    requested: str | None,
) -> str | None:
    preserved: str | None = None
    candidate = state.get("candidate")
    if isinstance(candidate, dict):
        base_sha = candidate.get("base_sha")
        candidate_head = candidate.get("head_sha")
        if (
            isinstance(base_sha, str)
            and isinstance(candidate_head, str)
            and run_git(root, "merge-base", "--is-ancestor", base_sha, candidate_head, check=False).returncode == 0
            and run_git(root, "merge-base", "--is-ancestor", candidate_head, head, check=False).returncode == 0
        ):
            preserved = base_sha
    if requested is None:
        return preserved
    resolved = run_git(root, "rev-parse", "--verify", f"{requested}^{{commit}}", check=False)
    requested_sha = resolved.stdout.strip()
    if resolved.returncode != 0:
        raise UsageError(f"Unknown review base: {requested}")
    if preserved is not None and requested_sha != preserved:
        raise IntegrityError("Requested review base conflicts with the preserved candidate base")
    return requested_sha


def candidate_ready(
    root: Path,
    *,
    change_id: str,
    base: str | None,
    addressed: list[str],
    noted: list[str],
    dry_run: bool,
) -> dict[str, Any]:
    state = load_state(root, change_id)
    head = git_head(root)
    if not head:
        raise IntegrityError("Candidate requires Git HEAD")
    if git_source_dirty_paths(root):
        return {"ok": True, "status": "blocked", "next_action": {"id": "commit-product-source"}}
    action = _require_decisions(root, state)
    if action:
        return {"ok": True, "status": "blocked", "next_action": action}
    dependency = dependency_status(root, state)
    if not dependency["satisfied"]:
        return {"ok": True, "status": "blocked", "next_action": next_action(root, state)}
    incomplete = sorted(
        ticket_id
        for ticket_id, item in state["tickets"].items()
        if item["status"] not in {"implemented", "validated", "done"}
    )
    if incomplete:
        return {
            "ok": True,
            "status": "blocked",
            "next_action": {"id": "continue-implementation", "tickets": incomplete},
        }
    commands, policy_digest = _validation_policy(root)
    profile = resolve_profile(root)
    source_digest = git_product_tree_digest(root)
    if source_digest is None:
        raise IntegrityError("Candidate source digest is unavailable")
    prior = state.get("review")
    if prior:
        base_sha = prior.get("head_sha")
    else:
        base_sha = _candidate_base(root, state, head=head, requested=base)
        if not base_sha:
            return {"ok": True, "status": "blocked", "next_action": {"id": "provide-review-base"}}
    active_ids = sorted(
        finding_id
        for finding_id, finding in state["findings"].items()
        if set(finding.get("blocks", [])) & {"review", "acceptance"}
        and finding.get("status") != "waived"
    )
    if set(addressed) & set(noted) or len(addressed) != len(set(addressed)) or len(noted) != len(set(noted)):
        raise UsageError("Finding dispositions must be unique")
    explicit = {item: "addressed" for item in addressed} | {item: "note" for item in noted}
    unknown = sorted(set(explicit) - set(active_ids))
    if unknown:
        raise UsageError("Unknown current findings: " + ", ".join(unknown))
    if active_ids and not explicit:
        current = {
            finding_id: state["findings"][finding_id].get("status")
            for finding_id in active_ids
            if state["findings"][finding_id].get("head_sha") == head
            and state["findings"][finding_id].get("status") in {"addressed", "note"}
        }
        explicit = current
    if set(explicit) != set(active_ids):
        return {
            "ok": True,
            "status": "blocked",
            "next_action": {"id": "declare-finding-disposition", "findings": active_ids},
        }
    contract = {
        "kind": "candidate",
        "change_id": change_id,
        "head_sha": head,
        "source_digest": source_digest,
        "definition_digest": definition_digest(root, state),
        "base_sha": base_sha,
        "policy_digest": policy_digest,
        "profile_digest": profile["digest"],
        "dispositions": explicit,
    }
    contract_digest = stable_digest(contract)
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, contract_digest))
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "status": "not-prepared",
            "run_id": run_id,
            "next_action": {"id": "run-candidate-ready"},
        }
    active, owner = _claim_run(
        root,
        state,
        run_id=run_id,
        kind="candidate",
        head_sha=head,
        contract_digest=contract_digest,
    )
    if not owner:
        if active.get("status") == "completed":
            current = load_state(root, change_id)
            candidate = current.get("candidate") or {}
            return {
                "ok": True,
                "status": "completed",
                "candidate_run_id": run_id,
                "review_pack_path": candidate.get("pack_path"),
                "next_action": {"id": "open-review-task"},
            }
        return {"ok": True, "status": "running", "next_action": {"id": "wait-candidate"}}
    evidence: list[dict[str, Any]] = []
    try:
        for command_id in commands:
            item = _run_validation(root, command_id)
            evidence.append({**item, "head_sha": head, "source_digest": source_digest})
            if item["status"] != "pass":
                _record_failure(
                    root,
                    change_id,
                    {
                        "kind": "validation",
                        "command_id": command_id,
                        "exit_code": item["exit_code"],
                        "diagnostic": item["diagnostic"],
                        "recorded_at": utc_now(),
                    },
                )
                _finish_run(root, change_id, run_id, status="failed", error=f"{command_id} failed")
                return {
                    "ok": True,
                    "status": "blocked",
                    "failed_command": command_id,
                    "diagnostic": item["diagnostic"],
                    "next_action": {"id": "fix-validation"},
                }
        current = load_state(root, change_id)
        if git_head(root) != head or git_product_tree_digest(root) != source_digest:
            raise IntegrityError("Candidate changed during validation")
        pack_state = copy.deepcopy(current)
        for finding_id, disposition in explicit.items():
            pack_state["findings"][finding_id]["status"] = disposition
            pack_state["findings"][finding_id]["head_sha"] = head
            pack_state["findings"][finding_id]["evidence"] = [
                item["command_id"] for item in evidence
            ]
        core = _pack_core(
            root,
            pack_state,
            kind="code",
            base_sha=str(base_sha),
            evidence=evidence,
            source_digest=source_digest,
        )
        pack, relative = _write_pack(root, core)

        def apply(value: dict[str, Any]) -> None:
            active_run = value.get("active_run")
            if not isinstance(active_run, dict) or active_run.get("run_id") != run_id:
                raise IntegrityError("Candidate run changed before commit")
            for finding_id, disposition in explicit.items():
                value["findings"][finding_id]["status"] = disposition
                value["findings"][finding_id]["head_sha"] = head
                value["findings"][finding_id]["evidence"] = [
                    item["command_id"] for item in evidence
                ]
            value["candidate"] = {
                "run_id": run_id,
                "status": "ready",
                "head_sha": head,
                "base_sha": base_sha,
                "source_digest": source_digest,
                "definition_digest": core["definition_digest"],
                "policy_digest": policy_digest,
                "profile_digest": profile["digest"],
                "evidence": evidence,
                "pack_path": relative,
                "pack_digest": pack["pack_digest"],
                "review_id": pack["review_id"],
            }
            value["phase"] = "review"
            value["lifecycle"] = "candidate-ready"
            active_run.update({"status": "completed", "completed_at": utc_now(), "pid": None, "lanes": {}})

        updated = mutate_state(root, change_id, apply)
        _clear_failure(root, change_id)
        return {
            "ok": True,
            "status": "completed",
            "candidate_run_id": run_id,
            "review_id": pack["review_id"],
            "review_pack_path": relative,
            "state_revision": updated["revision"],
            "next_action": {"id": "open-review-task"},
        }
    except Exception as exc:
        _finish_run(root, change_id, run_id, status="failed", error=str(exc))
        raise


def _decision_schema() -> dict[str, Any]:
    return read_json(SCHEMAS_ROOT / "review-decision.schema.json")


def _string_list(value: object, *, name: str, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise IntegrityError(f"{name} must be a string array")
    if len(value) != len(set(value)):
        raise IntegrityError(f"{name} must not contain duplicates")
    if allowed is not None and not set(value) <= allowed:
        raise IntegrityError(f"{name} contains unknown values")
    return value


def _validate_decision(value: object, *, pack: dict[str, Any]) -> dict[str, Any]:
    required = {
        "verdict",
        "summary",
        "findings",
        "ticket_verdicts",
        "requirement_verdicts",
        "prior_finding_verdicts",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise IntegrityError("Review decision fields are invalid")
    if value["verdict"] not in REVIEW_VERDICTS or not isinstance(value["summary"], str):
        raise IntegrityError("Review verdict or summary is invalid")
    tickets = set(pack["tickets"])
    requirements = set(pack["requirement_ids"])
    prior = {item["id"] for item in pack["prior_findings"]}
    findings: dict[str, dict[str, Any]] = {}
    finding_keys = {
        "id",
        "severity",
        "kind",
        "location",
        "issue",
        "impact",
        "required_fix",
        "ticket_ids",
        "requirement_ids",
        "blocks",
        "provenance",
    }
    if not isinstance(value["findings"], list):
        raise IntegrityError("findings must be an array")
    for item in value["findings"]:
        if not isinstance(item, dict) or set(item) != finding_keys:
            raise IntegrityError("Finding fields are invalid")
        finding_id = item["id"]
        if not isinstance(finding_id, str) or not finding_id or finding_id in findings:
            raise IntegrityError("Finding IDs must be unique strings")
        if item["severity"] not in SEVERITIES or item["kind"] not in FINDING_KINDS:
            raise IntegrityError(f"Invalid finding classification: {finding_id}")
        for key in ("location", "issue", "impact", "required_fix"):
            if not isinstance(item[key], str):
                raise IntegrityError(f"Finding {finding_id}.{key} must be text")
        location_path = item["location"].split(":", 1)[0]
        if Path(location_path).is_absolute() or ".." in Path(location_path).parts:
            raise IntegrityError(f"Finding {finding_id} has unsafe location")
        _string_list(item["ticket_ids"], name=f"{finding_id}.ticket_ids", allowed=tickets)
        _string_list(item["requirement_ids"], name=f"{finding_id}.requirement_ids", allowed=requirements)
        _string_list(item["blocks"], name=f"{finding_id}.blocks", allowed=BLOCKS)
        _string_list(item["provenance"], name=f"{finding_id}.provenance")
        findings[finding_id] = item

    def validate_verdicts(
        rows: object,
        *,
        id_key: str,
        allowed_ids: set[str],
        label: str,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(rows, list):
            raise IntegrityError(f"{label} must be an array")
        output: dict[str, dict[str, Any]] = {}
        expected = {id_key, "verdict", "finding_ids"}
        for row in rows:
            if not isinstance(row, dict) or set(row) != expected:
                raise IntegrityError(f"{label} fields are invalid")
            identifier = row[id_key]
            if identifier not in allowed_ids or identifier in output or row["verdict"] not in ITEM_VERDICTS:
                raise IntegrityError(f"{label} contains an invalid identifier or verdict")
            _string_list(row["finding_ids"], name=f"{label}.finding_ids", allowed=set(findings))
            if row["verdict"] != "clear" and not row["finding_ids"]:
                raise IntegrityError(f"{label} non-clear verdict must reference a finding")
            output[identifier] = row
        if set(output) != allowed_ids:
            raise IntegrityError(f"{label} must cover every permitted ID")
        return output

    validate_verdicts(
        value["ticket_verdicts"],
        id_key="ticket_id",
        allowed_ids=tickets,
        label="ticket_verdicts",
    )
    validate_verdicts(
        value["requirement_verdicts"],
        id_key="requirement_id",
        allowed_ids=requirements,
        label="requirement_verdicts",
    )
    prior_rows = value["prior_finding_verdicts"]
    if not isinstance(prior_rows, list):
        raise IntegrityError("prior_finding_verdicts must be an array")
    actual_prior: set[str] = set()
    for row in prior_rows:
        if not isinstance(row, dict) or set(row) != {
            "finding_id",
            "verdict",
            "replacement_finding_id",
            "evidence",
        }:
            raise IntegrityError("Prior finding verdict fields are invalid")
        finding_id = row["finding_id"]
        verdict = row["verdict"]
        replacement = row["replacement_finding_id"]
        if finding_id not in prior or finding_id in actual_prior or verdict not in PRIOR_VERDICTS:
            raise IntegrityError("Prior finding verdict is invalid")
        if verdict in {"still-open", "regressed"}:
            if replacement not in findings or replacement == finding_id:
                raise IntegrityError(f"Prior finding {finding_id} requires a replacement finding")
        elif replacement is not None:
            raise IntegrityError(f"Prior finding {finding_id} cannot declare a replacement")
        _string_list(row["evidence"], name=f"{finding_id}.evidence")
        actual_prior.add(finding_id)
    if actual_prior != prior:
        raise IntegrityError("Review decision must cover every prior finding")
    actionable = any(
        item["severity"] in {"blocker", "should-fix"} and "review" in item["blocks"]
        for item in findings.values()
    )
    if value["verdict"] == "review-clear" and actionable:
        raise IntegrityError("review-clear cannot contain a review-blocking finding")
    if value["verdict"] == "not-clear" and not actionable:
        raise IntegrityError("not-clear requires a review-blocking finding")
    return value


def _usage(transcript: bytes) -> dict[str, int | None]:
    latest: dict[str, int] = {}

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                } and isinstance(child, int):
                    latest[key] = max(latest.get(key, 0), child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for line in transcript.splitlines():
        try:
            visit(json.loads(line))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    input_tokens = latest.get("input_tokens")
    output_tokens = latest.get("output_tokens")
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": latest.get("cached_input_tokens"),
        "output_tokens": output_tokens,
        "reasoning_tokens": latest.get("reasoning_output_tokens"),
        "processed_tokens": (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        ),
    }


def _prompt(pack: dict[str, Any], *, lane: str, lens: str | None) -> str:
    projection = {
        key: pack[key]
        for key in (
            "kind",
            "change_id",
            "control_level",
            "impact_tags",
            "base_sha",
            "head_sha",
            "artifacts",
            "tickets",
            "requirement_ids",
            "evidence",
            "prior_findings",
            "platform_profile",
        )
    }
    scope = (
        "Review only the authored definition artifacts for completeness, contradictions, "
        "feasibility, architecture/design risk, and testable acceptance."
        if pack["kind"] == "definition"
        else "Review the exact Git diff base_sha..head_sha and its blast radius. Do not edit files."
    )
    return (
        "You are an independent read-only DLS reviewer. "
        + scope
        + "\nReturn only JSON matching the supplied schema. "
        "Use exact ticket and requirement IDs from the input. Cover every ticket, requirement, "
        "and prior finding. verified/waived require replacement_finding_id=null; "
        "still-open/regressed require a new complete finding and its ID. "
        "Release/production-only evidence must not block review unless the authored requirement "
        "explicitly places it there. Do not invent evidence. "
        f"Lane={lane}; lens={lens or 'general'}.\nINPUT:\n"
        + json.dumps(projection, ensure_ascii=False, sort_keys=True)
    )


def _codex_argv(
    *,
    workspace: Path,
    model: str,
    effort: str,
    output: Path,
) -> list[str]:
    executable = shutil.which("codex")
    if not executable:
        raise IntegrityError("Codex executable is unavailable")
    return [
        executable,
        "exec",
        "--model",
        model,
        "--config",
        f'model_reasoning_effort="{effort}"',
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--output-schema",
        str(SCHEMAS_ROOT / "review-decision.schema.json"),
        "--output-last-message",
        str(output),
        "--cd",
        str(workspace),
        "-",
    ]


def _model_call(
    *,
    workspace: Path,
    model: str,
    effort: str,
    prompt: str,
    lane_budget: int,
) -> tuple[Any, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="dls-model-") as temp:
        output = Path(temp) / "decision.json"
        last: dict[str, Any] | None = None
        completed_attempt = 0
        for attempt in (1, 2):
            completed_attempt = attempt
            result = _run_bounded(
                _codex_argv(workspace=workspace, model=model, effort=effort, output=output),
                cwd=workspace,
                environment=allowed_environment([]),
                timeout_seconds=MODEL_TIMEOUT_SECONDS,
                max_output_bytes=MODEL_TRANSCRIPT_BYTES,
                stdin=prompt.encode("utf-8"),
            )
            last = result
            if result["exit_code"] == 0 and output.is_file():
                break
            if attempt == 2:
                diagnostic = result["output"].decode("utf-8", errors="replace")[-2000:]
                diagnostic = diagnostic.replace(str(workspace), "<workspace>")
                diagnostic = re.sub(
                    r'("thread_id"\s*:\s*")[^"]+(")',
                    r"\1<redacted>\2",
                    diagnostic,
                )
                raise IntegrityError(
                    f"Review lane transport failed: exit={result['exit_code']}; "
                    f"diagnostic={diagnostic}"
                )
        assert last is not None
        try:
            raw_text = output.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise IntegrityError(f"Review lane output is unavailable: {exc}") from exc
        try:
            decision: Any = json.loads(raw_text)
            json_error = None
        except json.JSONDecodeError as exc:
            decision = raw_text
            json_error = str(exc)
        usage = _usage(last["output"])
        processed = usage["processed_tokens"]
        metadata = {
            "model": model,
            "effort": effort,
            "attempts": completed_attempt,
            "duration_seconds": round(last["duration_seconds"], 3),
            "transcript_digest": last["output_digest"],
            "output_digest": stable_digest(decision),
            "usage": usage,
            "budget": {
                "target_tokens": lane_budget,
                "processed_tokens": processed,
                "over_target": isinstance(processed, int) and processed > lane_budget,
            },
        }
        if json_error:
            metadata["json_error"] = json_error
        return decision, metadata


def _repair(
    *,
    raw: Any,
    error: str,
    pack: dict[str, Any],
    effort: str,
    budget: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle = {
        "contract": REPAIR_CONTRACT,
        "raw_decision": raw,
        "validation_error": error[:4000],
        "ticket_ids": sorted(pack["tickets"]),
        "requirement_ids": pack["requirement_ids"],
        "prior_findings": pack["prior_findings"],
    }
    prompt = (
        "Repair only the JSON structure/reference error below. Preserve the global verdict and "
        "finding IDs, classification, issue, impact, required fix, and references; do not invent "
        "findings or change semantic verdicts. Ticket and requirement verdicts evaluate the reviewed "
        "definition/code, not authored lifecycle status. Every non-clear item verdict must reference "
        "a finding; change lifecycle-derived blocked/not-clear rows with no finding to clear. A global "
        "not-clear verdict requires at least one blocker or should-fix finding that blocks review; if "
        "such an existing actionable finding omitted only that block, add review to its blocks. A "
        "global review-clear verdict cannot contain a review-blocking actionable finding. Apply all "
        "of these cross-field rules in the same repair and return one complete decision matching the "
        "schema.\n"
        + json.dumps(bundle, ensure_ascii=False, sort_keys=True)
    )
    with tempfile.TemporaryDirectory(prefix="dls-repair-workspace-") as temp:
        return _model_call(
            workspace=Path(temp),
            model=MODEL_SECONDARY,
            effort=effort,
            prompt=prompt,
            lane_budget=budget,
        )


def _lane(
    root: Path,
    *,
    run_id: str,
    pack: dict[str, Any],
    workspace: Path,
    lane: str,
    model: str,
    effort: str,
    lens: str | None,
    budget: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state(root, pack["change_id"])
    cached = (state.get("active_run") or {}).get("lanes", {}).get(lane)
    if isinstance(cached, dict) and cached.get("status") == "completed":
        return cached["decision"], cached["metadata"]
    if isinstance(cached, dict) and cached.get("status") == "failed":
        raise IntegrityError(f"Review lane {lane} previously failed: {cached.get('error')}")
    if isinstance(cached, dict) and cached.get("status") == "needs-repair":
        raw = cached["raw"]
        metadata = cached["metadata"]
        validation_error = cached["error"]
    else:
        _set_lane(
            root,
            pack["change_id"],
            run_id,
            lane,
            {"status": "running", "model": model, "effort": effort, "started_at": utc_now()},
        )
        try:
            raw, metadata = _model_call(
                workspace=workspace,
                model=model,
                effort=effort,
                prompt=_prompt(pack, lane=lane, lens=lens),
                lane_budget=budget,
            )
        except Exception as exc:
            _set_lane(
                root,
                pack["change_id"],
                run_id,
                lane,
                {"status": "failed", "error": str(exc), "completed_at": utc_now()},
            )
            raise
        try:
            decision = _validate_decision(raw, pack=pack)
            validation_error = None
        except IntegrityError as exc:
            validation_error = str(exc)
            _set_lane(
                root,
                pack["change_id"],
                run_id,
                lane,
                {
                    "status": "needs-repair",
                    "raw": raw,
                    "error": validation_error,
                    "metadata": metadata,
                },
            )
    repair_metadata = None
    if validation_error is not None:
        repair_lane = f"{lane}:repair"
        current = load_state(root, pack["change_id"])
        cached_repair = (current.get("active_run") or {}).get("lanes", {}).get(repair_lane)
        if isinstance(cached_repair, dict) and cached_repair.get("status") == "completed":
            decision = cached_repair["decision"]
            repair_metadata = cached_repair["metadata"]
        elif (
            isinstance(cached_repair, dict)
            and cached_repair.get("status") == "failed"
            and cached_repair.get("contract") == REPAIR_CONTRACT
        ):
            raise IntegrityError(f"Review repair previously failed: {cached_repair.get('error')}")
        else:
            _set_lane(
                root,
                pack["change_id"],
                run_id,
                repair_lane,
                {"status": "running", "contract": REPAIR_CONTRACT, "started_at": utc_now()},
            )
            try:
                repaired, repair_metadata = _repair(
                    raw=raw,
                    error=validation_error,
                    pack=pack,
                    effort="xhigh" if pack["control_level"] == "critical" else "high",
                    budget=BUDGETS[pack["control_level"]]["repair"],
                )
                decision = _validate_decision(repaired, pack=pack)
                repair_metadata["contract"] = REPAIR_CONTRACT
            except Exception as exc:
                _set_lane(
                    root,
                    pack["change_id"],
                    run_id,
                    repair_lane,
                    {
                        "status": "failed",
                        "contract": REPAIR_CONTRACT,
                        "error": str(exc),
                        "completed_at": utc_now(),
                    },
                )
                raise
            _set_lane(
                root,
                pack["change_id"],
                run_id,
                repair_lane,
                {
                    "status": "completed",
                    "contract": REPAIR_CONTRACT,
                    "decision": decision,
                    "metadata": repair_metadata,
                    "completed_at": utc_now(),
                },
            )
    if repair_metadata:
        metadata["repair"] = repair_metadata
    _set_lane(
        root,
        pack["change_id"],
        run_id,
        lane,
        {
            "status": "completed",
            "decision": decision,
            "metadata": metadata,
            "completed_at": utc_now(),
        },
    )
    return decision, metadata


def _secondary_lens(pack: dict[str, Any]) -> tuple[str, str] | None:
    if pack["control_level"] != "critical":
        return None
    tags = set(pack["impact_tags"])
    for triggers, lens, effort in RISK_LENSES:
        if tags & triggers:
            return lens, effort
    return None


def _actionable(value: dict[str, Any]) -> bool:
    return any(
        item["severity"] in {"blocker", "should-fix"} and "review" in item["blocks"]
        for item in value["findings"]
    )


def _conflicts(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # Independent reviewers may discover different additive findings. A clear
    # row on one side and a supported non-clear row on the other is merged
    # conservatively; it is not a semantic contradiction requiring another
    # model call.
    left_prior = {item["finding_id"]: item["verdict"] for item in left["prior_finding_verdicts"]}
    right_prior = {item["finding_id"]: item["verdict"] for item in right["prior_finding_verdicts"]}
    if left_prior != right_prior:
        return True
    left_findings = {
        (item["location"], item["issue"], item["impact"], item["required_fix"]): (
            item["severity"],
            tuple(item["blocks"]),
        )
        for item in left["findings"]
    }
    right_findings = {
        (item["location"], item["issue"], item["impact"], item["required_fix"]): (
            item["severity"],
            tuple(item["blocks"]),
        )
        for item in right["findings"]
    }
    shared = set(left_findings) & set(right_findings)
    return any(left_findings[key] != right_findings[key] for key in shared)


def _canonicalize(value: dict[str, Any], change_id: str) -> dict[str, Any]:
    output = json.loads(json.dumps(value))
    mapping: dict[str, str] = {}
    for finding in output["findings"]:
        original = finding["id"]
        semantic = {key: finding[key] for key in finding if key != "id"}
        mapping[original] = f"{change_id}-R{stable_digest(semantic)[:10].upper()}"
        finding["id"] = mapping[original]
    for field in ("ticket_verdicts", "requirement_verdicts"):
        for row in output[field]:
            row["finding_ids"] = [mapping[item] for item in row["finding_ids"]]
    for row in output["prior_finding_verdicts"]:
        replacement = row["replacement_finding_id"]
        if replacement is not None:
            row["replacement_finding_id"] = mapping[replacement]
    return output


def _merge(left: dict[str, Any], right: dict[str, Any] | None, change_id: str) -> dict[str, Any]:
    left = _canonicalize(left, change_id)
    if right is None:
        return left
    right = _canonicalize(right, change_id)
    findings = {item["id"]: item for item in [*left["findings"], *right["findings"]]}
    actionable = _actionable({"findings": list(findings.values())})

    verdict_rank = {"clear": 0, "not-clear": 1, "blocked": 2}

    def merge_rows(field: str, key: str) -> list[dict[str, Any]]:
        left_rows = {item[key]: item for item in left[field]}
        right_rows = {item[key]: item for item in right[field]}
        output: list[dict[str, Any]] = []
        for identifier in sorted(set(left_rows) | set(right_rows)):
            first = left_rows.get(identifier)
            second = right_rows.get(identifier)
            rows = [item for item in (first, second) if item is not None]
            verdict = max(
                (item["verdict"] for item in rows),
                key=lambda item: verdict_rank[item],
            )
            finding_ids = sorted(
                {
                    finding_id
                    for item in rows
                    for finding_id in item["finding_ids"]
                }
            )
            output.append({key: identifier, "verdict": verdict, "finding_ids": finding_ids})
        return output

    return {
        "verdict": "not-clear" if actionable else (
            "blocked" if "blocked" in {left["verdict"], right["verdict"]} else "review-clear"
        ),
        "summary": " ".join(dict.fromkeys([left["summary"], right["summary"]])),
        "findings": list(findings.values()),
        "ticket_verdicts": merge_rows("ticket_verdicts", "ticket_id"),
        "requirement_verdicts": merge_rows("requirement_verdicts", "requirement_id"),
        "prior_finding_verdicts": left["prior_finding_verdicts"],
    }


def _reconcile(
    root: Path,
    run_id: str,
    pack: dict[str, Any],
    left: dict[str, Any],
    right: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = load_state(root, pack["change_id"])
    cached = (state.get("active_run") or {}).get("lanes", {}).get("reconciliation")
    if isinstance(cached, dict) and cached.get("status") == "completed":
        return cached["decision"], cached["metadata"]
    if isinstance(cached, dict) and cached.get("status") == "failed":
        raise IntegrityError(f"Reconciliation previously failed: {cached.get('error')}")
    prompt = (
        "Resolve only the direct structured disagreement between two independent reviews. "
        "Do not read product source or invent new evidence. Return one complete decision.\n"
        + json.dumps(
            {"pack": pack, "primary": left, "secondary": right},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    _set_lane(
        root,
        pack["change_id"],
        run_id,
        "reconciliation",
        {"status": "running", "started_at": utc_now()},
    )
    try:
        with tempfile.TemporaryDirectory(prefix="dls-reconcile-") as temp:
            raw, metadata = _model_call(
                workspace=Path(temp),
                model=MODEL_SECONDARY,
                effort="high",
                prompt=prompt,
                lane_budget=BUDGETS["critical"]["reconciliation"],
            )
        try:
            decision = _validate_decision(raw, pack=pack)
        except IntegrityError as exc:
            repair_lane = "reconciliation:repair"
            current = load_state(root, pack["change_id"])
            cached_repair = (current.get("active_run") or {}).get("lanes", {}).get(repair_lane)
            if isinstance(cached_repair, dict) and cached_repair.get("status") == "completed":
                decision = cached_repair["decision"]
                repair_metadata = cached_repair["metadata"]
            elif (
                isinstance(cached_repair, dict)
                and cached_repair.get("status") == "failed"
                and cached_repair.get("contract") == REPAIR_CONTRACT
            ):
                raise IntegrityError(
                    f"Reconciliation repair previously failed: {cached_repair.get('error')}"
                )
            else:
                _set_lane(
                    root,
                    pack["change_id"],
                    run_id,
                    repair_lane,
                    {"status": "running", "contract": REPAIR_CONTRACT, "started_at": utc_now()},
                )
                try:
                    repaired, repair_metadata = _repair(
                        raw=raw,
                        error=str(exc),
                        pack=pack,
                        effort="xhigh",
                        budget=BUDGETS["critical"]["repair"],
                    )
                    decision = _validate_decision(repaired, pack=pack)
                    repair_metadata["contract"] = REPAIR_CONTRACT
                except Exception as repair_error:
                    _set_lane(
                        root,
                        pack["change_id"],
                        run_id,
                        repair_lane,
                        {
                            "status": "failed",
                            "contract": REPAIR_CONTRACT,
                            "error": str(repair_error),
                            "completed_at": utc_now(),
                        },
                    )
                    raise
                _set_lane(
                    root,
                    pack["change_id"],
                    run_id,
                    repair_lane,
                    {
                        "status": "completed",
                        "contract": REPAIR_CONTRACT,
                        "decision": decision,
                        "metadata": repair_metadata,
                        "completed_at": utc_now(),
                    },
                )
            metadata["repair"] = repair_metadata
    except Exception as exc:
        _set_lane(
            root,
            pack["change_id"],
            run_id,
            "reconciliation",
            {"status": "failed", "error": str(exc), "completed_at": utc_now()},
        )
        raise
    _set_lane(
        root,
        pack["change_id"],
        run_id,
        "reconciliation",
        {
            "status": "completed",
            "decision": decision,
            "metadata": metadata,
            "completed_at": utc_now(),
        },
    )
    return decision, metadata


def _workspace(root: Path, head: str) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    holder = tempfile.TemporaryDirectory(prefix="dls-review-worktree-")
    path = Path(holder.name)
    result = run_git(root, "worktree", "add", "--detach", str(path), head, check=False)
    if result.returncode != 0:
        holder.cleanup()
        raise IntegrityError(result.stderr.strip() or "Unable to create review workspace")
    return holder, path


def _remove_workspace(root: Path, holder: tempfile.TemporaryDirectory[str], path: Path) -> None:
    run_git(root, "worktree", "remove", "--force", str(path), check=False)
    holder.cleanup()


def _load_code_pack(root: Path, state: dict[str, Any]) -> tuple[dict[str, Any], str]:
    candidate = current_candidate(root, state)
    if candidate is None:
        raise IntegrityError("Candidate is not prepared")
    relative = candidate.get("pack_path")
    if not isinstance(relative, str):
        raise IntegrityError("Candidate ReviewPack is missing")
    path = safe_resolve(root, relative, must_exist=True)
    pack = read_json(path)
    if (
        pack.get("schema_version") != PACK_SCHEMA
        or pack.get("contract") != PACK_CONTRACT
        or stable_digest({key: value for key, value in pack.items() if key != "pack_digest"})
        != pack.get("pack_digest")
        or pack.get("pack_digest") != candidate.get("pack_digest")
    ):
        raise IntegrityError("Candidate ReviewPack failed integrity validation")
    return pack, relative


def _definition_pack(
    root: Path,
    state: dict[str, Any],
    *,
    write: bool,
) -> tuple[dict[str, Any], str]:
    head = git_head(root)
    if not head:
        raise IntegrityError("Definition review requires Git HEAD")
    source = git_product_tree_digest(root)
    if source is None:
        raise IntegrityError("Source digest unavailable")
    core = _pack_core(
        root,
        state,
        kind="definition",
        base_sha=head,
        evidence=[],
        source_digest=source,
    )
    core["prior_findings"] = []
    return _write_pack(root, core, write=write)


def _review_result(
    pack: dict[str, Any],
    decision: dict[str, Any],
    reviewers: list[dict[str, Any]],
    reconciliation: dict[str, Any] | None,
    routing: dict[str, Any],
) -> dict[str, Any]:
    profile = {
        key: pack["platform_profile"][key]
        for key in ("contract", "name", "digest")
    }
    return {
        "schema_version": RESULT_SCHEMA,
        "contract": RESULT_CONTRACT,
        "review_id": pack["review_id"],
        "change_id": pack["change_id"],
        "kind": pack["kind"],
        "base_sha": pack["base_sha"],
        "head_sha": pack["head_sha"],
        "pack_digest": pack["pack_digest"],
        "definition_digest": pack["definition_digest"],
        "platform_profile": profile,
        "verdict": decision["verdict"],
        "summary": decision["summary"],
        "findings": decision["findings"],
        "ticket_verdicts": decision["ticket_verdicts"],
        "requirement_verdicts": decision["requirement_verdicts"],
        "prior_finding_verdicts": decision["prior_finding_verdicts"],
        "reviewers": reviewers,
        "reconciliation": reconciliation,
        "routing": routing,
    }


def _processed_tokens(metadata: dict[str, Any]) -> list[int]:
    output: list[int] = []
    usage = metadata.get("usage")
    if isinstance(usage, dict) and isinstance(usage.get("processed_tokens"), int):
        output.append(usage["processed_tokens"])
    repair = metadata.get("repair")
    if isinstance(repair, dict):
        output.extend(_processed_tokens(repair))
    return output


def _recoverable_budget_primary(
    state: dict[str, Any],
    pack: dict[str, Any],
    *,
    kind: str,
) -> dict[str, Any] | None:
    """Recover a validated actionable primary from the pre-v4 budget dead end."""

    active = state.get("active_run")
    if not isinstance(active, dict) or active.get("status") != "failed":
        return None
    if active.get("kind") != f"review:{kind}" or active.get("head_sha") != pack["head_sha"]:
        return None
    error = str(active.get("error") or "")
    if "budget" not in error.lower():
        return None
    lane = (active.get("lanes") or {}).get("primary")
    if not isinstance(lane, dict) or lane.get("status") != "completed":
        return None
    decision = lane.get("decision")
    metadata = lane.get("metadata")
    if not isinstance(decision, dict) or not isinstance(metadata, dict):
        return None
    validated = _validate_decision(copy.deepcopy(decision), pack=pack)
    if not _actionable(validated):
        return None
    if metadata.get("output_digest") != stable_digest(decision):
        raise IntegrityError("Recovered primary decision digest changed")
    recovered = copy.deepcopy(lane)
    recovered_metadata = recovered.setdefault("metadata", {})
    recovered_metadata["recovery"] = {
        "contract": "dls-budget-recovery/v1",
        "reason": "actionable-primary-before-optional-budget-failure",
        "source_run_digest": stable_digest(
            {
                "run_id": active.get("run_id"),
                "contract_digest": active.get("contract_digest"),
                "head_sha": active.get("head_sha"),
            }
        ),
    }
    return recovered


def review_run(
    root: Path,
    *,
    change_id: str,
    kind: str,
    stream: Callable[[dict[str, Any]], None] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if kind not in {"definition", "code"}:
        raise UsageError("review-run --kind must be definition or code")
    state = load_state(root, change_id)
    head = git_head(root)
    if not head:
        raise IntegrityError("Review requires Git HEAD")
    if git_source_dirty_paths(root):
        return {"ok": True, "status": "not-prepared", "next_action": {"id": "commit-product-source"}}
    existing = (
        state.get("definition_review")
        if kind == "definition"
        else current_review(root, state)
    )
    profile_digest = resolve_profile(root)["digest"]
    if (
        isinstance(existing, dict)
        and existing.get("head_sha") == head
        and existing.get("definition_digest") == definition_digest(root, state)
        and existing.get("profile_digest", profile_digest) == profile_digest
        and existing.get("result_path")
    ):
        action = next_action(root, state)
        return {
            "ok": True,
            "status": "completed",
            "review_id": existing["review_id"],
            "verdict": existing["verdict"],
            "review_result_path": existing["result_path"],
            "delivery_receipt": receipt(root, state),
            "next_action": action,
            "human_decision": human_decision(root, state, action=action),
        }
    if kind == "code":
        action = _require_decisions(root, state)
        if action:
            return {
                "ok": True,
                "status": "not-prepared",
                "review_id": None,
                "verdict": None,
                "review_result_path": None,
                "next_action": action,
                "human_decision": human_decision(root, state, action=action),
            }
        pack, pack_path = _load_code_pack(root, state)
    else:
        if state["change"]["control"] in {"micro", "routine"}:
            action = next_action(root, state)
            return {
                "ok": True,
                "status": "completed",
                "verdict": "review-clear",
                "review_result_path": None,
                "next_action": action,
                "human_decision": human_decision(root, state, action=action),
            }
        pack, pack_path = _definition_pack(root, state, write=not dry_run)
    if (
        pack["head_sha"] != head
        or pack["definition_digest"] != definition_digest(root, state)
        or pack["source_digest"] != git_product_tree_digest(root)
    ):
        raise IntegrityError("ReviewPack is not current for HEAD and definition")
    contract_digest = stable_digest(
        {
            "kind": "review",
            "pack_digest": pack["pack_digest"],
            "runner_contract": RUNNER_CONTRACT,
            "model_execution_contract": MODEL_EXECUTION_CONTRACT,
        }
    )
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, contract_digest))
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "status": "not-prepared",
            "review_id": pack["review_id"],
            "review_pack_path": pack_path,
            "next_action": {"id": "run-review"},
        }
    recovered_primary = _recoverable_budget_primary(state, pack, kind=kind)
    active, owner = _claim_run(
        root,
        state,
        run_id=run_id,
        kind=f"review:{kind}",
        head_sha=head,
        contract_digest=contract_digest,
    )
    if not owner:
        if active.get("status") == "completed":
            current = load_state(root, change_id)
            completed_review = current.get(
                "definition_review" if kind == "definition" else "review"
            ) or {}
            action = next_action(root, current)
            return {
                "ok": True,
                "status": "completed",
                "review_id": completed_review.get("review_id"),
                "verdict": completed_review.get("verdict"),
                "review_result_path": completed_review.get("result_path"),
                "delivery_receipt": receipt(root, current),
                "next_action": action,
                "human_decision": human_decision(root, current, action=action),
            }
        return {
            "ok": True,
            "status": "running",
            "review_id": pack["review_id"],
            "next_action": {"id": "wait-review"},
        }
    if recovered_primary is not None and "primary" not in active.get("lanes", {}):
        _set_lane(root, change_id, run_id, "primary", recovered_primary)
    if stream:
        stream(
            {
                "event": "started",
                "terminal": False,
                "status": "running",
                "review_id": pack["review_id"],
                "kind": kind,
                "next_action": {"id": "wait-process"},
            }
        )
    holder, workspace = _workspace(root, head)
    try:
        control = pack["control_level"]
        selected = _secondary_lens(pack)
        routing: dict[str, Any] = {
            "contract": ROUTING_CONTRACT,
            "planned": ["primary"] + (["secondary"] if selected else []),
            "completed": [],
            "skipped": [],
            "recovered": [],
        }
        if recovered_primary is not None:
            routing["recovered"].append(
                {"lane": "primary", "reason": "prior-budget-failure"}
            )
        primary, primary_meta = _lane(
            root,
            run_id=run_id,
            pack=pack,
            workspace=workspace,
            lane="primary",
            model=MODEL_PRIMARY,
            effort="high",
            lens=None,
            budget=BUDGETS[control]["primary"],
        )
        routing["completed"].append("primary")
        if stream:
            stream(
                {
                    "event": "lane-transition",
                    "terminal": False,
                    "lane": "primary",
                    "status": "completed",
                }
            )
        secondary = None
        secondary_meta = None
        if selected and _actionable(primary):
            routing["skipped"].append(
                {"lane": "secondary", "reason": "actionable-primary"}
            )
            if stream:
                stream(
                    {
                        "event": "review-short-circuited",
                        "terminal": False,
                        "reason": "actionable-primary",
                        "skipped_lane": "secondary",
                    }
                )
        elif selected:
            lens, effort = selected
            secondary, secondary_meta = _lane(
                root,
                run_id=run_id,
                pack=pack,
                workspace=workspace,
                lane="secondary",
                model=MODEL_SECONDARY,
                effort=effort,
                lens=lens,
                budget=BUDGETS["critical"]["secondary"],
            )
            routing["completed"].append("secondary")
            if stream:
                stream(
                    {
                        "event": "lane-transition",
                        "terminal": False,
                        "lane": "secondary",
                        "status": "completed",
                    }
                )
        reconciliation_meta = None
        if secondary is not None and _conflicts(primary, secondary):
            routing["planned"].append("reconciliation")
            decision, reconciliation_meta = _reconcile(root, run_id, pack, primary, secondary)
            routing["completed"].append("reconciliation")
        else:
            decision = _merge(primary, secondary, change_id)
        reviewers = [{"lane": "primary", **primary_meta}]
        if secondary_meta:
            reviewers.append({"lane": "secondary", **secondary_meta})
        all_usage = [
            token
            for item in reviewers
            for token in _processed_tokens(item)
        ]
        if reconciliation_meta:
            all_usage.extend(_processed_tokens(reconciliation_meta))
        processed_tokens = sum(all_usage) if all_usage else None
        aggregate_target = BUDGETS[control]["aggregate"]
        aggregate_over_target = (
            isinstance(processed_tokens, int) and processed_tokens > aggregate_target
        )
        routing["budget"] = {
            "aggregate_target_tokens": aggregate_target,
            "processed_tokens": processed_tokens,
            "over_target": aggregate_over_target,
        }
        if aggregate_over_target and not _actionable(decision):
            raise IntegrityError("Review aggregate budget exhausted before clearance")
        result = _review_result(
            pack,
            decision,
            reviewers,
            reconciliation_meta,
            routing,
        )
        relative = f".dls/reviews/{change_id}/results/{pack['review_id']}.json"
        path = safe_resolve(root, relative)
        if path.is_file() and read_json(path) != result:
            raise IntegrityError("ReviewIR collision")
        if not path.is_file():
            atomic_write_json(path, result, backup=False)
        result_digest = stable_digest(result)

        def apply(value: dict[str, Any]) -> None:
            active_run = value.get("active_run")
            if not isinstance(active_run, dict) or active_run.get("run_id") != run_id:
                raise IntegrityError("Review run changed before import")
            record = {
                "review_id": pack["review_id"],
                "kind": kind,
                "head_sha": head,
                "base_sha": pack["base_sha"],
                "definition_digest": pack["definition_digest"],
                "source_digest": pack["source_digest"],
                "profile_digest": pack["platform_profile"]["digest"],
                "platform_profile": {
                    key: pack["platform_profile"][key]
                    for key in ("contract", "name", "digest")
                },
                "decision_digests": pack["decision_digests"],
                "verdict": result["verdict"],
                "result_path": relative,
                "result_digest": result_digest,
                "pack_path": pack_path,
                "pack_digest": pack["pack_digest"],
                "usage": {
                    "processed_tokens": processed_tokens,
                    "reviewers": reviewers,
                    "routing": routing,
                },
            }
            if kind == "definition":
                value["definition_review"] = record
                value["phase"] = "definition"
                value["lifecycle"] = (
                    "definition-reviewed" if result["verdict"] == "review-clear" else "not-clear"
                )
            else:
                value["review"] = record
                value["phase"] = "review"
                value["lifecycle"] = result["verdict"]
                value["candidate"] = None
            value["findings"] = {
                item["id"]: {
                    **item,
                    "status": "open",
                    "review_id": pack["review_id"],
                }
                for item in result["findings"]
            }
            active_run.clear()
            active_run.update(
                {
                    "run_id": run_id,
                    "kind": f"review:{kind}",
                    "head_sha": head,
                    "contract_digest": contract_digest,
                    "status": "completed",
                    "completed_at": utc_now(),
                    "pid": None,
                    "lanes": {},
                }
            )

        updated = mutate_state(root, change_id, apply)
        _clear_failure(root, change_id)
        action = next_action(root, updated)
        output = {
            "ok": True,
            "status": "completed",
            "review_id": pack["review_id"],
            "verdict": result["verdict"],
            "review_result_path": relative,
            "delivery_receipt": receipt(root, updated),
            "next_action": action,
            "human_decision": human_decision(root, updated, action=action),
        }
        if stream:
            stream({"event": "completed", "terminal": True, **output})
        return output
    except Exception as exc:
        _record_failure(
            root,
            change_id,
            {"kind": f"review:{kind}", "error": str(exc), "recorded_at": utc_now()},
        )
        failed_state = _finish_run(root, change_id, run_id, status="failed", error=str(exc))
        if stream:
            error = str(exc)
            stream(
                {
                    "event": "completed",
                    "terminal": True,
                    "status": "failed",
                    "review_id": pack["review_id"],
                    "verdict": None,
                    "review_result_path": None,
                    "error": error,
                    "next_action": next_action(root, failed_state),
                }
            )
        raise
    finally:
        _remove_workspace(root, holder, workspace)
