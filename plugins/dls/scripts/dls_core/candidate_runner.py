"""Deterministic implementation/remediation candidate orchestration."""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import IntegrityError, LockError, UsageError
from .io import read_json, safe_resolve, sha256_bytes, utc_now
from .operations import (
    _active_prior_findings,
    _load_remediation_manifest,
    _latest_dispositions,
    _process_is_alive,
    _prior_review_link,
    _review_pack_state_entry,
    _validate_review_pack,
    review_pack,
    validate_command,
)
from .repo import (
    command_config,
    command_contract_digest,
    git_head,
    git_source_dirty_paths,
    git_source_snapshot_digest,
    is_git_repo,
    load_config,
    resolve_profile,
    run_git,
)
from .state import StateStore, current_definition_digest, derived_approval_statuses
from .worktrees import resolve_registered_worktree


CANDIDATE_RUN_CONTRACT = "dls-candidate-run/v2"
CANDIDATE_DIAGNOSTIC_LIMIT = 6 * 1024
CANDIDATE_FAILURE_EXCERPT_LIMIT = 4 * 1024


def _owner_root(root: Path, change_id: str) -> tuple[Path, str]:
    candidate = root.resolve()
    if (
        (candidate / ".dls" / "config.toml").is_file()
        and StateStore(candidate).path(change_id).is_file()
    ):
        return candidate, "current-checkout"
    return resolve_registered_worktree(candidate, change_id), "registered-worktree"


def _next_action(identifier: str, detail: str) -> dict[str, str]:
    return {"id": identifier, "detail": detail}


def _blocked(
    *,
    change_id: str,
    owner: Path,
    owner_selection: str,
    action: str,
    detail: str,
    dry_run: bool,
    run_id: str | None = None,
    failed_command: str | None = None,
    failure_excerpt: str | None = None,
    log_path: str | None = None,
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if task_context is None:
        from .telemetry import unavailable_task_context

        task_context = unavailable_task_context("implementation")
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": dry_run,
        "changed": False,
        "change_id": change_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "run_id": run_id,
        "status": "blocked",
        "phase": "preflight" if run_id is None else "validating",
        "next_action": _next_action(action, detail),
        "review_pack_path": None,
        "task_context": task_context,
    }
    if failed_command is not None:
        result["failed_command"] = failed_command
    if failure_excerpt:
        result["failure_excerpt"] = failure_excerpt[-4096:]
    if log_path:
        result["log_path"] = log_path
    return result


def _policy_commands(root: Path, extras: list[str]) -> tuple[list[str], str]:
    config = load_config(root)
    policy = config.get("policy", {})
    required = list(policy.get("review_required_commands", []))
    if not required:
        return [], ""
    ordered: list[str] = []
    for command_id in [*required, *extras]:
        if command_id not in ordered:
            command_config(root, command_id)
            ordered.append(command_id)
    contract = {
        command_id: command_contract_digest(root, command_id)
        for command_id in ordered
    }
    digest = sha256_bytes(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return ordered, digest


def _successful_evidence(
    root: Path,
    state: dict[str, Any],
    *,
    command_id: str,
    head_sha: str,
    source_digest: str,
    contract_digest: str,
) -> tuple[str, dict[str, Any]] | None:
    for relative in reversed(state.get("evidence", [])):
        if not isinstance(relative, str):
            continue
        record = read_json(safe_resolve(root, relative, must_exist=True))
        extra = record.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        if (
            record.get("command_id") == command_id
            and record.get("git_sha") == head_sha
            and record.get("source_digest") == source_digest
            and record.get("exit_code") == 0
            and not extra.get("timed_out", False)
            and not extra.get("output_overflow", False)
            and extra.get("command_contract_digest") == contract_digest
        ):
            return relative, record
    return None


def _current_definition_approval(root: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    definition_digest = current_definition_digest(root, state)
    return next(
        (
            item
            for item in reversed(derived_approval_statuses(root, state))
            if item.get("decision") == "definition"
            and item.get("status") == "current"
            and item.get("object_digest") == definition_digest
        ),
        None,
    )


def _finding_overrides(
    root: Path,
    state: dict[str, Any],
    *,
    addressed: list[str],
    noted: list[str],
) -> tuple[dict[str, str], list[str]]:
    if len(addressed) != len(set(addressed)) or len(noted) != len(set(noted)):
        raise UsageError("Candidate finding IDs must not contain duplicates")
    overlap = sorted(set(addressed) & set(noted))
    if overlap:
        raise UsageError("Findings cannot be both addressed and note: " + ",".join(overlap))
    active = _active_prior_findings(root, state, include_waived=True)
    actionable = {
        item["finding_id"]: item
        for item in active
        if not (
            isinstance(item.get("disposition"), dict)
            and item["disposition"].get("status") == "waived"
        )
    }
    declared = set(addressed) | set(noted)
    unknown = sorted(declared - set(actionable))
    if unknown:
        raise UsageError("Candidate references non-actionable findings: " + ",".join(unknown))
    statuses = {finding_id: "addressed" for finding_id in addressed}
    statuses.update({finding_id: "note" for finding_id in noted})
    return statuses, sorted(actionable)


def _declaration_digest(statuses: dict[str, str]) -> str:
    return sha256_bytes(
        json.dumps(statuses, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _ancestor_distance(root: Path, ancestor: str, descendant: str) -> int | None:
    exists = run_git(root, "cat-file", "-e", f"{ancestor}^{{commit}}", check=False)
    if exists.returncode != 0:
        return None
    ancestry = run_git(
        root,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        check=False,
    )
    if ancestry.returncode != 0:
        return None
    value = run_git(root, "rev-list", "--count", f"{ancestor}..{descendant}").stdout.strip()
    try:
        return int(value)
    except ValueError as exc:
        raise IntegrityError("Unable to measure candidate run ancestry") from exc


def _eligible_declaration_run(
    root: Path,
    state: dict[str, Any],
    *,
    head_sha: str,
    definition_digest: str,
    prior_review_id: str,
    prior_review_result_digest: str,
    manifest_digest: str,
    policy_digest: str,
    profile_digest: str,
    active_finding_ids: list[str],
) -> dict[str, Any] | None:
    eligible: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(state.get("candidate_runs", [])):
        if not isinstance(item, dict):
            continue
        statuses = item.get("finding_dispositions")
        run_head = item.get("head_sha")
        if (
            item.get("candidate_run_contract") != CANDIDATE_RUN_CONTRACT
            or item.get("review_mode") != "remediation"
            or item.get("status") not in {"blocked", "failed", "completed"}
            or item.get("canonical_review_id") != prior_review_id
            or item.get("canonical_review_result_digest")
            != prior_review_result_digest
            or item.get("remediation_manifest_digest") != manifest_digest
            or item.get("definition_digest") != definition_digest
            or item.get("policy_digest") != policy_digest
            or item.get("platform_profile_digest") != profile_digest
            or item.get("active_finding_ids") != active_finding_ids
            or not isinstance(run_head, str)
            or not isinstance(statuses, dict)
            or item.get("declaration_digest") != _declaration_digest(statuses)
            or sorted(statuses) != active_finding_ids
            or any(value not in {"addressed", "note"} for value in statuses.values())
        ):
            continue
        distance = _ancestor_distance(root, run_head, head_sha)
        if distance is not None:
            eligible.append((distance, -index, item))
    if not eligible:
        return None
    return copy.deepcopy(min(eligible, key=lambda value: (value[0], value[1]))[2])


def _current_head_declaration(
    state: dict[str, Any],
    *,
    head_sha: str,
    active_finding_ids: list[str],
) -> dict[str, str] | None:
    latest = _latest_dispositions(state)
    statuses: dict[str, str] = {}
    for finding_id in active_finding_ids:
        disposition = latest.get(finding_id)
        if (
            not isinstance(disposition, dict)
            or disposition.get("status") not in {"addressed", "note"}
            or disposition.get("git_sha") != head_sha
        ):
            return None
        statuses[finding_id] = disposition["status"]
    return statuses


def _exact_head_pack(
    root: Path,
    state: dict[str, Any],
    *,
    run: dict[str, Any],
    current_head: str,
) -> dict[str, Any] | None:
    if run.get("status") != "completed" or run.get("head_sha") != current_head:
        return None
    relative = run.get("review_pack_path")
    if not isinstance(relative, str):
        raise IntegrityError("Completed candidate run is missing ReviewPack path")
    pack = read_json(safe_resolve(root, relative, must_exist=True))
    _validate_review_pack(pack, state["change_id"])
    current_profile = resolve_profile(root, config=load_config(root))
    recorded_profile_digest = run.get("platform_profile_digest")
    pack_profile = pack.get("platform_profile")
    if (
        isinstance(recorded_profile_digest, str)
        and recorded_profile_digest != current_profile["digest"]
    ):
        return None
    if (
        isinstance(pack_profile, dict)
        and pack_profile.get("digest") != current_profile["digest"]
    ):
        return None
    if (
        pack.get("head_sha") != current_head
        or pack.get("review_id") != run.get("review_id")
        or pack.get("pack_digest") != run.get("pack_digest")
    ):
        raise IntegrityError("Candidate ReviewPack does not match its exact-HEAD run")
    entry = next(
        (
            item
            for item in state["reviews"]
            if isinstance(item, dict)
            and item.get("kind") == "pack"
            and item.get("review_id") == pack["review_id"]
            and item.get("pack_path") == relative
        ),
        None,
    )
    if entry is None or entry.get("pack_digest") != pack["pack_digest"]:
        raise IntegrityError("Candidate ReviewPack is not intact in DLS state")
    return pack


def _validation_failure(
    *,
    command_id: str,
    evidence_path: str | None,
    evidence: dict[str, Any],
    excerpt: str,
    log_path: str | None,
) -> dict[str, Any]:
    bounded_excerpt = excerpt[-CANDIDATE_FAILURE_EXCERPT_LIMIT:]
    extra = evidence.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    result: dict[str, Any] = {
        "command_id": command_id,
        "exit_code": evidence.get("exit_code"),
        "evidence_path": evidence_path,
        "redacted_log_path": log_path,
        "excerpt": bounded_excerpt,
        "excerpt_digest": sha256_bytes(bounded_excerpt.encode("utf-8")),
        "output_sha256": extra.get("output_sha256"),
        "recorded_at": utc_now(),
    }
    return result


def _disposition_records(
    *,
    change_id: str,
    run_id: str,
    head_sha: str,
    statuses: dict[str, str],
    evidence_paths: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for finding_id, status in sorted(statuses.items()):
        record = {
            "id": str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"dls:{change_id}:candidate:{run_id}:{finding_id}:{status}",
                )
            ),
            "finding_id": finding_id,
            "status": status,
            "rationale": (
                f"Candidate {head_sha[:12]} claims the required fix is addressed; "
                "independent review must verify it."
                if status == "addressed"
                else (
                    f"Candidate {head_sha[:12]} claims no closure; independent review "
                    "must adjudicate the finding and its stage."
                )
            ),
            "git_sha": head_sha,
            "evidence": list(evidence_paths) if status == "addressed" else [],
            "actor": "codex",
            "authority": "workflow",
            "prompt": None,
            "response": None,
            "recorded_at": utc_now(),
        }
        records.append(record)
    return records


def _run_contract(
    *,
    change_id: str,
    head_sha: str,
    source_digest: str,
    definition_digest: str,
    base_ref: str,
    prior_review_id: str | None,
    manifest_digest: str | None,
    policy_digest: str,
    profile_digest: str,
    statuses: dict[str, str],
) -> tuple[str, str]:
    value = {
        "change_id": change_id,
        "head_sha": head_sha,
        "source_digest": source_digest,
        "definition_digest": definition_digest,
        "base_ref": base_ref,
        "prior_review_id": prior_review_id,
        "manifest_digest": manifest_digest,
        "policy_digest": policy_digest,
        "profile_digest": profile_digest,
        "finding_dispositions": statuses,
    }
    digest = sha256_bytes(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return digest, str(uuid.uuid5(uuid.NAMESPACE_URL, f"dls:candidate:{digest}"))


def _run_response(
    *,
    change_id: str,
    owner: Path,
    owner_selection: str,
    run: dict[str, Any],
    diagnostic: bool = False,
    current_head: str | None = None,
    exact_head: bool | None = None,
    prepared: bool | None = None,
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = run.get("status", "running")
    if status == "completed":
        action = _next_action("open-review-task", "candidate is ready")
    elif status == "blocked":
        action = run.get("next_action") or _next_action("fix-validation", "validation failed")
    elif status == "failed":
        action = _next_action("retry-infrastructure", run.get("failure_reason", "pipeline failed"))
    else:
        action = _next_action("wait-candidate", f"candidate run {run.get('run_id')} is active")
    commands = run.get("commands", [])
    completed = [
        item.get("command_id")
        for item in commands
        if isinstance(item, dict) and item.get("status") in {"completed", "reused"}
    ]
    remaining = [
        item.get("command_id")
        for item in commands
        if isinstance(item, dict) and item.get("status") not in {"completed", "reused"}
    ]
    started_at = run.get("started_at")
    elapsed_seconds: float | None = None
    if isinstance(started_at, str):
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            finished_value = run.get("completed_at")
            finished = (
                datetime.fromisoformat(finished_value.replace("Z", "+00:00"))
                if isinstance(finished_value, str)
                else datetime.now(timezone.utc)
            )
            elapsed_seconds = max(0.0, round((finished - started).total_seconds(), 3))
        except ValueError:
            elapsed_seconds = None
    if current_head is None:
        current_head = git_head(owner)
    if exact_head is None:
        exact_head = run.get("head_sha") == current_head
    if prepared is None:
        prepared = bool(status == "completed" and exact_head and run.get("review_pack_path"))
    if status == "completed" and not prepared:
        action = _next_action(
            "run-candidate-ready",
            "historical candidate run is not a prepared exact-HEAD handoff",
        )
    if task_context is None:
        task_context = run.get("task_context")
    if not isinstance(task_context, dict):
        from .telemetry import unavailable_task_context

        task_context = unavailable_task_context("implementation")
    result: dict[str, Any] = {
        "ok": True,
        "dry_run": False,
        "changed": False,
        "change_id": change_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "run_id": run.get("run_id"),
        "operation_id": run.get("operation_id"),
        "status": status,
        "phase": run.get("phase"),
        "active_command": run.get("active_command"),
        "elapsed_seconds": elapsed_seconds,
        "updated_at": run.get("updated_at"),
        "completed_commands": completed,
        "remaining_commands": remaining,
        "review_pack_path": run.get("review_pack_path"),
        "review_id": run.get("review_id"),
        "current_head": current_head,
        "candidate_head": run.get("head_sha"),
        "exact_head": exact_head,
        "prepared": prepared,
        "failed_command": run.get("failed_command"),
        "next_action": action,
        "task_context": task_context,
    }
    if diagnostic and isinstance(run.get("validation_failure"), dict):
        failure = copy.deepcopy(run["validation_failure"])
        result["validation_failure"] = failure
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        excerpt = failure.get("excerpt")
        while len(encoded) > CANDIDATE_DIAGNOSTIC_LIMIT and isinstance(excerpt, str) and excerpt:
            overflow = len(encoded) - CANDIDATE_DIAGNOSTIC_LIMIT
            excerpt = excerpt[min(len(excerpt), max(64, overflow)) :]
            failure["excerpt"] = excerpt
            result["validation_failure"] = failure
            encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > CANDIDATE_DIAGNOSTIC_LIMIT:
            raise IntegrityError("Candidate diagnostic response exceeds bounded limit")
    return result


def _run_routine_review(
    owner: Path,
    *,
    change_id: str,
    run: dict[str, Any],
    finding_counts: dict[str, int] | None = None,
    declaration_source: str | None = None,
) -> dict[str, Any]:
    review_pack_path = run.get("review_pack_path")
    run_id = run.get("run_id")
    if not isinstance(review_pack_path, str) or not isinstance(run_id, str):
        raise IntegrityError("Completed routine candidate is missing its ReviewPack")
    from .review_runner import review_run

    reviewed = review_run(
        owner,
        change_id=change_id,
        pack_path=str(safe_resolve(owner, review_pack_path, must_exist=True)),
        operation_id=f"candidate:{run_id}:routine-review",
    )
    reviewed["candidate_run_id"] = run_id
    dispositions = run.get("finding_dispositions", {})
    reviewed["candidate_ready"] = {
        "review_pack_path": review_pack_path,
        "finding_counts": finding_counts
        if finding_counts is not None
        else {
            "addressed": sum(
                value == "addressed" for value in dispositions.values()
            ),
            "note": sum(value == "note" for value in dispositions.values()),
        },
        "declaration_source": declaration_source
        if declaration_source is not None
        else run.get("declaration_source"),
    }
    return reviewed


def _claim_with_retry(
    store: StateStore,
    change_id: str,
    candidate_run: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Absorb the millisecond state-lock race, not a long-running pipeline."""
    for attempt in range(50):
        try:
            return store.claim_candidate_run(
                change_id,
                candidate_run=candidate_run,
            )
        except LockError:
            if attempt == 49:
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")


def _candidate_ready_impl(
    root: Path,
    *,
    change_id: str,
    base_ref: str | None,
    addressed: list[str],
    noted: list[str],
    extra_commands: list[str],
    operation_id: str | None,
    dry_run: bool = False,
    bind_context: bool = True,
) -> dict[str, Any]:
    owner, owner_selection = _owner_root(root, change_id)
    if not is_git_repo(owner):
        raise IntegrityError("Candidate readiness requires Git")
    store = StateStore(owner)
    state = store.load(change_id)
    dirty = git_source_dirty_paths(owner)
    if dirty:
        return _blocked(
            change_id=change_id,
            owner=owner,
            owner_selection=owner_selection,
            action="commit-product-source",
            detail="dirty=" + ",".join(dirty),
            dry_run=dry_run,
        )
    head_sha = git_head(owner)
    source_digest = git_source_snapshot_digest(owner)
    if not head_sha or not source_digest:
        raise IntegrityError("Candidate readiness requires an exact Git source snapshot")
    definition_digest = current_definition_digest(owner, state)
    if _current_definition_approval(owner, state) is None:
        return _blocked(
            change_id=change_id,
            owner=owner,
            owner_selection=owner_selection,
            action="approve-definition",
            detail=f"definition_digest={definition_digest}",
            dry_run=dry_run,
        )
    incomplete = sorted(
        ticket_id
        for ticket_id, ticket in state["tickets"].items()
        if ticket.get("status") not in {"implemented", "validated", "done"}
    )
    if incomplete:
        return _blocked(
            change_id=change_id,
            owner=owner,
            owner_selection=owner_selection,
            action="implement-tickets",
            detail="incomplete=" + ",".join(incomplete),
            dry_run=dry_run,
        )
    commands, policy_digest = _policy_commands(owner, extra_commands)
    if not commands:
        return _blocked(
            change_id=change_id,
            owner=owner,
            owner_selection=owner_selection,
            action="configure-review-commands",
            detail="policy.review_required_commands must list trusted command IDs",
            dry_run=dry_run,
        )
    platform_profile = resolve_profile(owner, config=load_config(owner))
    profile_digest = platform_profile["digest"]
    profile_drifted = any(
        isinstance(item, dict)
        and item.get("head_sha") == head_sha
        and isinstance(item.get("platform_profile_digest"), str)
        and item.get("platform_profile_digest") != profile_digest
        for item in state.get("candidate_runs", [])
    )
    prior_review, prior_report = _prior_review_link(owner, state)
    review_mode = "remediation" if prior_review is not None else "full"
    effective_base = base_ref
    remediation_manifest: tuple[str, dict[str, Any]] | None = None
    parent_run: dict[str, Any] | None = None
    declaration_source = "explicit"
    active_finding_ids: list[str] = []
    if prior_review is None:
        if addressed or noted:
            raise UsageError("Initial candidate cannot declare review findings")
        if not effective_base:
            return _blocked(
                change_id=change_id,
                owner=owner,
                owner_selection=owner_selection,
                action="provide-review-base",
                detail="first candidate requires --base BASE",
                dry_run=dry_run,
            )
        statuses: dict[str, str] = {}
    else:
        if head_sha == prior_review["head_sha"]:
            return _blocked(
                change_id=change_id,
                owner=owner,
                owner_selection=owner_selection,
                action="commit-remediation",
                detail="candidate HEAD still equals previous reviewed HEAD",
                dry_run=dry_run,
            )
        remediation_manifest = _load_remediation_manifest(
            owner,
            change_id=change_id,
            prior_review=prior_review,
        )
        effective_base = (
            base_ref
            or prior_report.get("epic_base_sha")
            or prior_report.get("base_sha")
        )
        overrides, active_finding_ids = _finding_overrides(
            owner,
            state,
            addressed=addressed,
            noted=noted,
        )
        manifest_digest = remediation_manifest[1]["manifest_digest"]
        current_head_statuses = _current_head_declaration(
            state,
            head_sha=head_sha,
            active_finding_ids=active_finding_ids,
        )
        parent_run = _eligible_declaration_run(
            owner,
            state,
            head_sha=head_sha,
            definition_digest=definition_digest,
            prior_review_id=prior_review["review_id"],
            prior_review_result_digest=prior_review["result_digest"],
            manifest_digest=manifest_digest,
            policy_digest=policy_digest,
            profile_digest=profile_digest,
            active_finding_ids=active_finding_ids,
        )
        if set(overrides) == set(active_finding_ids):
            statuses = overrides
        elif current_head_statuses is not None:
            statuses = {**current_head_statuses, **overrides}
            declaration_source = "current-head-state" if not overrides else "mixed"
        elif parent_run is not None:
            inherited = parent_run["finding_dispositions"]
            statuses = {**inherited, **overrides}
            declaration_source = "inherited" if not overrides else "mixed"
        else:
            missing_count = len(set(active_finding_ids) - set(overrides))
            return _blocked(
                change_id=change_id,
                owner=owner,
                owner_selection=owner_selection,
                action="declare-finding-disposition",
                detail=(
                    f"missing_count={missing_count}; "
                    f"remediation_manifest_path={remediation_manifest[0]}"
                ),
                dry_run=dry_run,
            )
    if not isinstance(effective_base, str) or not effective_base:
        raise IntegrityError("Candidate review base is missing")
    effective_base = run_git(owner, "rev-parse", effective_base).stdout.strip()
    manifest_digest = (
        remediation_manifest[1]["manifest_digest"]
        if remediation_manifest is not None
        else None
    )
    contract_digest, run_id = _run_contract(
        change_id=change_id,
        head_sha=head_sha,
        source_digest=source_digest,
        definition_digest=definition_digest,
        base_ref=str(effective_base),
        prior_review_id=prior_review.get("review_id") if prior_review else None,
        manifest_digest=manifest_digest,
        policy_digest=policy_digest,
        profile_digest=profile_digest,
        statuses=statuses,
    )
    effective_operation_id = operation_id or f"candidate-ready:{run_id}"
    if operation_id is not None:
        conflict = next(
            (
                item
                for item in state.get("candidate_runs", [])
                if isinstance(item, dict)
                and item.get("operation_id") == operation_id
                and item.get("run_id") != run_id
            ),
            None,
        )
        if conflict is not None:
            raise IntegrityError(
                "Candidate operation ID already belongs to another candidate contract"
            )
    parent_run_id = parent_run.get("run_id") if parent_run is not None else None
    if parent_run_id == run_id:
        parent_run_id = parent_run.get("parent_run_id")
    declaration_digest = _declaration_digest(statuses)
    from .telemetry import candidate_task_context

    task_context = candidate_task_context(
        owner,
        change_id=change_id,
        operation_id=effective_operation_id,
        definition_digest=definition_digest,
        review_base_sha=str(effective_base),
        canonical_review_id=prior_review.get("review_id") if prior_review else None,
        canonical_review_result_digest=(
            prior_review.get("result_digest") if prior_review else None
        ),
        remediation_manifest_digest=manifest_digest,
        record=bind_context and not dry_run,
    )
    command_records = [
        {
            "command_id": command_id,
            "status": "pending",
            "attempts": 0,
            "contract_digest": command_contract_digest(owner, command_id),
        }
        for command_id in commands
    ]
    projected = {
        "ok": True,
        "dry_run": True,
        "changed": False,
        "change_id": change_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "run_id": run_id,
        "operation_id": effective_operation_id,
        "status": "projected",
        "phase": "preflight",
        "review_mode": review_mode,
        "head_sha": head_sha,
        "commands": commands,
        "finding_counts": {
            "addressed": sum(value == "addressed" for value in statuses.values()),
            "note": sum(value == "note" for value in statuses.values()),
        },
        "declaration_source": declaration_source,
        "review_pack_path": None,
        "next_action": _next_action("run-candidate-ready", "execute trusted validation"),
        "task_context": task_context,
    }
    if dry_run:
        return projected
    _, claimed_run, claimed = _claim_with_retry(
        store,
        change_id,
        {
            "run_id": run_id,
            "contract_digest": contract_digest,
            "operation_id": effective_operation_id,
            "runner_pid": os.getpid(),
            "status": "running",
            "phase": "preflight",
            "active_command": None,
            "head_sha": head_sha,
            "source_digest": source_digest,
            "definition_digest": definition_digest,
            "review_base_sha": str(effective_base),
            "review_mode": review_mode,
            "candidate_run_contract": CANDIDATE_RUN_CONTRACT,
            "parent_run_id": parent_run_id,
            "canonical_review_id": prior_review.get("review_id") if prior_review else None,
            "canonical_review_result_digest": (
                prior_review.get("result_digest") if prior_review else None
            ),
            "remediation_manifest_digest": manifest_digest,
            "policy_digest": policy_digest,
            "platform_profile_contract": platform_profile["contract"],
            "platform_profile_name": platform_profile["name"],
            "platform_profile_digest": profile_digest,
            "active_finding_ids": active_finding_ids,
            "declaration_digest": declaration_digest,
            "declaration_source": declaration_source,
            "commands": command_records,
            "finding_dispositions": statuses,
            "started_at": utc_now(),
            "task_context": task_context,
        },
    )
    if not claimed:
        if (
            claimed_run.get("status") == "running"
            and not _process_is_alive(claimed_run.get("runner_pid"))
        ):
            store.update_candidate_run(
                change_id,
                run_id=claimed_run["run_id"],
                updates={
                    "status": "failed",
                    "phase": "failed",
                    "failure_reason": "candidate runner process is no longer alive",
                    "completed_at": utc_now(),
                },
            )
            _, claimed_run, claimed = _claim_with_retry(
                store,
                change_id,
                {
                    **claimed_run,
                    "operation_id": effective_operation_id,
                    "runner_pid": os.getpid(),
                },
            )
        if not claimed:
            if (
                state.get("control_level") == "routine"
                and claimed_run.get("status") == "completed"
            ):
                return _run_routine_review(
                    owner,
                    change_id=change_id,
                    run=claimed_run,
                )
            prepared = False
            if claimed_run.get("status") == "completed":
                current_state = store.load(change_id)
                prepared = _exact_head_pack(
                    owner,
                    current_state,
                    run=claimed_run,
                    current_head=head_sha,
                ) is not None
            return _run_response(
                change_id=change_id,
                owner=owner,
                owner_selection=owner_selection,
                run=claimed_run,
                current_head=head_sha,
                exact_head=True,
                prepared=prepared,
                task_context=task_context,
            )
    evidence_paths: list[str] = []
    for command_id in commands:
        state = store.load(change_id)
        contract = command_contract_digest(owner, command_id)
        reusable = None
        if not profile_drifted:
            reusable = _successful_evidence(
                owner,
                state,
                command_id=command_id,
                head_sha=head_sha,
                source_digest=source_digest,
                contract_digest=contract,
            )
        if reusable is not None:
            evidence_paths.append(reusable[0])
            _, claimed_run, _ = store.update_candidate_run(
                change_id,
                run_id=run_id,
                updates={
                    "phase": "validating",
                    "active_command": None,
                    "commands": [
                        (
                            {**item, "status": "reused", "evidence_path": reusable[0]}
                            if item.get("command_id") == command_id
                            else item
                        )
                        for item in claimed_run["commands"]
                    ],
                },
            )
            continue
        current_item = next(
            item for item in claimed_run["commands"] if item["command_id"] == command_id
        )
        attempt = int(current_item.get("attempts", 0)) + 1
        updated_commands = [
            (
                {**item, "status": "running", "attempts": attempt}
                if item.get("command_id") == command_id
                else item
            )
            for item in claimed_run["commands"]
        ]
        _, claimed_run, _ = store.update_candidate_run(
            change_id,
            run_id=run_id,
            updates={
                "phase": "validating",
                "active_command": command_id,
                "commands": updated_commands,
            },
        )
        state = store.load(change_id)
        validation = validate_command(
            owner,
            change_id=change_id,
            command_id=command_id,
            expected_revision=state["state_revision"],
            operation_id=f"candidate:{run_id}:validate:{command_id}:{attempt}",
        )
        if git_head(owner) != head_sha or git_source_snapshot_digest(owner) != source_digest:
            store.update_candidate_run(
                change_id,
                run_id=run_id,
                updates={
                    "status": "failed",
                    "phase": "failed",
                    "active_command": None,
                    "failure_reason": "source changed during candidate validation",
                    "completed_at": utc_now(),
                },
            )
            raise IntegrityError("Product source changed during candidate validation")
        evidence_path = validation.get("evidence_path")
        if not validation["ok"]:
            evidence = validation.get("evidence", {})
            summary = evidence.get("summary", "") if isinstance(evidence, dict) else ""
            excerpt = summary.partition("\nexcerpt=")[2]
            log_path = validation.get("validation", {}).get("redacted_log_path")
            diagnostic = _validation_failure(
                command_id=command_id,
                evidence_path=evidence_path if isinstance(evidence_path, str) else None,
                evidence=evidence if isinstance(evidence, dict) else {},
                excerpt=excerpt,
                log_path=log_path if isinstance(log_path, str) else None,
            )
            if validation.get("validation", {}).get("spawn_error"):
                store.update_candidate_run(
                    change_id,
                    run_id=run_id,
                    updates={
                        "status": "failed",
                        "phase": "failed",
                        "active_command": None,
                        "failed_command": command_id,
                        "failure_reason": f"unable to start validation command {command_id}",
                        "validation_failure": diagnostic,
                        "completed_at": utc_now(),
                    },
                )
                raise IntegrityError(f"Unable to start validation command: {command_id}")
            failed_commands = [
                (
                    {
                        **item,
                        "status": "failed",
                        "evidence_path": evidence_path,
                    }
                    if item.get("command_id") == command_id
                    else item
                )
                for item in claimed_run["commands"]
            ]
            next_action = _next_action("fix-validation", f"command={command_id}")
            store.update_candidate_run(
                change_id,
                run_id=run_id,
                updates={
                    "status": "blocked",
                    "phase": "validating",
                    "active_command": None,
                    "commands": failed_commands,
                    "next_action": next_action,
                    "failed_command": command_id,
                    "validation_failure": diagnostic,
                    "completed_at": utc_now(),
                },
            )
            return _blocked(
                change_id=change_id,
                owner=owner,
                owner_selection=owner_selection,
                action="fix-validation",
                detail=f"command={command_id}",
                dry_run=False,
                run_id=run_id,
                failed_command=command_id,
                failure_excerpt=excerpt,
                log_path=log_path,
                task_context=task_context,
            )
        if not isinstance(evidence_path, str):
            raise IntegrityError(f"Validation produced no evidence path: {command_id}")
        evidence_paths.append(evidence_path)
        completed_commands = [
            (
                {**item, "status": "completed", "evidence_path": evidence_path}
                if item.get("command_id") == command_id
                else item
            )
            for item in claimed_run["commands"]
        ]
        _, claimed_run, _ = store.update_candidate_run(
            change_id,
            run_id=run_id,
            updates={
                "active_command": None,
                "commands": completed_commands,
            },
        )
    state = store.load(change_id)
    _, current_policy_digest = _policy_commands(owner, extra_commands)
    current_profile_digest = resolve_profile(
        owner,
        config=load_config(owner),
    )["digest"]
    if (
        git_head(owner) != head_sha
        or git_source_snapshot_digest(owner) != source_digest
        or current_definition_digest(owner, state) != definition_digest
        or current_policy_digest != policy_digest
        or current_profile_digest != profile_digest
    ):
        store.update_candidate_run(
            change_id,
            run_id=run_id,
            updates={
                "status": "failed",
                "phase": "failed",
                "failure_reason": "candidate contract drifted before finalization",
                "completed_at": utc_now(),
            },
        )
        raise IntegrityError("Candidate contract drifted before finalization")
    dispositions = _disposition_records(
        change_id=change_id,
        run_id=run_id,
        head_sha=head_sha,
        statuses=statuses,
        evidence_paths=evidence_paths,
    )
    _, _, _ = store.update_candidate_run(
        change_id,
        run_id=run_id,
        updates={"phase": "recording-dispositions", "active_command": None},
    )
    state = store.load(change_id)
    staged = copy.deepcopy(state)
    staged["finding_dispositions"].extend(dispositions)
    pack_operation_id = f"candidate:{run_id}:pack"
    comparison_ref = prior_review["head_sha"] if prior_review else effective_base
    pack_preview = review_pack(
        owner,
        change_id=change_id,
        base_ref=str(effective_base),
        head_ref=None,
        expected_revision=state["state_revision"],
        advisory_dirty=False,
        operation_id=pack_operation_id,
        dry_run=True,
        _review_mode=review_mode,
        _comparison_ref=str(comparison_ref),
        _prior_review=prior_review,
        _prior_report=prior_report,
        _remediation_manifest=remediation_manifest,
        _operation_kind="candidate-ready",
        _state_override=staged,
        _skip_review_gate=True,
    )
    pack = pack_preview["review_pack"]
    relative_pack_path = f".dls/reviews/{change_id}/packs/{pack['review_id']}.json"
    _, _, _ = store.update_candidate_run(
        change_id,
        run_id=run_id,
        updates={"phase": "creating-pack"},
    )
    state = store.load(change_id)

    def mutate(value: dict[str, Any]) -> None:
        value["finding_dispositions"].extend(dispositions)
        value["reviews"].append(_review_pack_state_entry(pack, relative_pack_path))
        value["phase"] = "review"
        run = next(
            item
            for item in value.setdefault("candidate_runs", [])
            if item.get("run_id") == run_id
        )
        run.update(
            {
                "status": "completed",
                "phase": "completed",
                "active_command": None,
                "review_id": pack["review_id"],
                "review_pack_path": relative_pack_path,
                "pack_digest": pack["pack_digest"],
                "completed_at": utc_now(),
                "updated_at": utc_now(),
            }
        )

    updated, changed = store.mutate_with_immutable_artifact(
        change_id,
        expected_revision=state["state_revision"],
        operation_id=f"candidate:{run_id}:finalize",
        operation_kind="candidate-ready-finalize",
        artifact_path=safe_resolve(owner, relative_pack_path),
        artifact_value=pack,
        mutator=mutate,
    )
    completed_run = next(
        item for item in updated.get("candidate_runs", []) if item.get("run_id") == run_id
    )
    result = _run_response(
        change_id=change_id,
        owner=owner,
        owner_selection=owner_selection,
        run=completed_run,
        task_context=task_context,
    )
    result["changed"] = changed
    result["finding_counts"] = {
        "addressed": sum(value == "addressed" for value in statuses.values()),
        "note": sum(value == "note" for value in statuses.values()),
    }
    result["declaration_source"] = declaration_source
    try:
        from .telemetry import cache_prune

        cache_prune(owner, change_id=change_id, apply=True)
    except Exception as exc:  # cleanup must never break delivery
        result["cleanup_warning"] = str(exc)
    if updated.get("control_level") == "routine":
        # Routine delivery deliberately keeps one independent Terra review in
        # this implementation task. Import lazily to avoid a module cycle.
        return _run_routine_review(
            owner,
            change_id=change_id,
            run=completed_run,
            finding_counts=result["finding_counts"],
            declaration_source=declaration_source,
        )
    return result


def candidate_ready(
    root: Path,
    *,
    change_id: str,
    base_ref: str | None,
    addressed: list[str],
    noted: list[str],
    extra_commands: list[str],
    operation_id: str | None,
    dry_run: bool = False,
    _bind_task_context: bool = True,
) -> dict[str, Any]:
    """Run candidate orchestration and never leave this process recorded as live."""
    try:
        return _candidate_ready_impl(
            root,
            change_id=change_id,
            base_ref=base_ref,
            addressed=addressed,
            noted=noted,
            extra_commands=extra_commands,
            operation_id=operation_id,
            dry_run=dry_run,
            bind_context=_bind_task_context,
        )
    except Exception as exc:
        if not dry_run:
            try:
                owner, _ = _owner_root(root, change_id)
                store = StateStore(owner)
                state = store.load(change_id)
                active = next(
                    (
                        item
                        for item in reversed(state.get("candidate_runs", []))
                        if isinstance(item, dict)
                        and item.get("status") == "running"
                        and item.get("runner_pid") == os.getpid()
                    ),
                    None,
                )
                if active is not None:
                    store.update_candidate_run(
                        change_id,
                        run_id=active["run_id"],
                        updates={
                            "status": "failed",
                            "phase": "failed",
                            "active_command": None,
                            "failure_reason": str(exc)[:2000],
                            "completed_at": utc_now(),
                        },
                    )
            except Exception:
                pass
        raise


def candidate_status(
    root: Path,
    *,
    change_id: str,
    operation_id: str | None = None,
    diagnostic: bool = False,
    _inspect_task_context: bool = True,
) -> dict[str, Any]:
    owner, owner_selection = _owner_root(root, change_id)
    state = StateStore(owner).load(change_id)
    current_head = git_head(owner)
    runs = [item for item in state.get("candidate_runs", []) if isinstance(item, dict)]
    if operation_id is not None:
        runs = [item for item in runs if item.get("operation_id") == operation_id]
    else:
        runs = [item for item in runs if item.get("head_sha") == current_head]
    if not runs:
        from .telemetry import unavailable_task_context

        return {
            "ok": True,
            "change_id": change_id,
            "owner_root": str(owner),
            "owner_selection": owner_selection,
            "run_id": None,
            "status": "idle",
            "phase": None,
            "active_command": None,
            "completed_commands": [],
            "remaining_commands": [],
            "review_pack_path": None,
            "review_id": None,
            "current_head": current_head,
            "candidate_head": None,
            "exact_head": False,
            "prepared": False,
            "next_action": _next_action("run-candidate-ready", "no candidate run exists"),
            "task_context": unavailable_task_context("implementation"),
        }
    selected = runs[-1]
    exact_head = selected.get("head_sha") == current_head
    prepared = False
    if exact_head and selected.get("status") == "completed":
        prepared = _exact_head_pack(
            owner,
            state,
            run=selected,
            current_head=current_head,
        ) is not None
    if _inspect_task_context:
        from .telemetry import candidate_task_context

        inspected_context = candidate_task_context(
            owner,
            change_id=change_id,
            operation_id=str(selected.get("operation_id") or "candidate-status"),
            definition_digest=selected.get("definition_digest"),
            review_base_sha=selected.get("review_base_sha"),
            canonical_review_id=selected.get("canonical_review_id"),
            canonical_review_result_digest=selected.get(
                "canonical_review_result_digest"
            ),
            remediation_manifest_digest=selected.get("remediation_manifest_digest"),
            record=False,
        )
    else:
        from .telemetry import unavailable_task_context

        inspected_context = unavailable_task_context("implementation")
    response = _run_response(
        change_id=change_id,
        owner=owner,
        owner_selection=owner_selection,
        run=selected,
        diagnostic=diagnostic,
        current_head=current_head,
        exact_head=exact_head,
        prepared=prepared,
        task_context=inspected_context,
    )
    if not diagnostic:
        for key in ("active_command", "failed_command"):
            if response.get(key) is None:
                response.pop(key, None)
        if not response.get("remaining_commands"):
            response.pop("remaining_commands", None)
    return response
