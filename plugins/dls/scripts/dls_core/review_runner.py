"""End-to-end, state-owned DLS review orchestration."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import IntegrityError
from .io import (
    atomic_write_json,
    atomic_write_text,
    read_json,
    safe_resolve,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from .operations import (
    NATIVE_REVIEW_MAX_OUTPUT_BYTES,
    NATIVE_REVIEW_TIMEOUT_SECONDS,
    NATIVE_REVIEW_TRANSCRIPT_MAX_BYTES,
    RETRYABLE_REVIEW_LANE_STATUSES,
    REVIEW_LANE_MAX_ATTEMPTS,
    REVIEW_RUNNER_CONTRACT,
    _attempt_lease_expired,
    _codex_usage_from_output,
    _existing_remediation_manifest_path,
    _finding_blocks,
    _latest_review_result,
    _process_is_alive,
    _read_review_result,
    _review_lane_entries,
    _semantic_review_effort,
    _validate_review_pack_current,
    _validate_review_report,
    _run_bounded_command,
    review_import,
    review_ready,
    review_start,
)
from .repo import (
    PLUGIN_ROOT,
    SCHEMAS_ROOT,
    allowed_environment,
    git_head,
    git_source_snapshot_digest,
    run_git,
)
from .review_presentation import build_review_presentation
from .state import StateStore
from .worktrees import resolve_registered_worktree

SEMANTIC_MODEL = "gpt-5.6-sol"
SPECIALIST_MODEL = "gpt-5.6-terra"
SPECIALIST_EFFORT = "high"
REVIEW_PROMPTS_ROOT = PLUGIN_ROOT / "assets" / "review-prompts"


def _validate_strict_output_schema(value: Any, *, location: str = "$") -> None:
    """Reject schemas that the Responses API strict validator will reject.

    Codex structured output requires every declared object property to be
    required and forbids additional properties. Validating this locally keeps a
    mechanical schema defect from consuming a model lane and failing remotely.
    """
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_strict_output_schema(item, location=f"{location}[{index}]")
        return
    if not isinstance(value, dict):
        return
    properties = value.get("properties")
    if isinstance(properties, dict):
        required = value.get("required")
        if not isinstance(required, list) or set(required) != set(properties):
            missing = sorted(set(properties) - set(required or []))
            extra = sorted(set(required or []) - set(properties))
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if extra:
                details.append("extra=" + ",".join(extra))
            raise IntegrityError(
                f"Strict output schema required mismatch at {location}: "
                + ("; ".join(details) or "required must equal properties")
            )
        if value.get("additionalProperties") is not False:
            raise IntegrityError(
                f"Strict output schema must set additionalProperties=false at {location}"
            )
    for key, item in value.items():
        _validate_strict_output_schema(item, location=f"{location}.{key}")


def _lane_contract_digest(
    *,
    pack: dict[str, Any],
    lane_key: str,
    model: str,
    effort: str,
    prompt_digest: str,
    schema_digest: str,
    context_digest: str,
) -> str:
    contract = {
        "runner_contract": REVIEW_RUNNER_CONTRACT,
        "review_id": pack["review_id"],
        "lane_key": lane_key,
        "pack_digest": pack["pack_digest"],
        "head_sha": pack["head_sha"],
        "model": model,
        "reasoning_effort": effort,
        "prompt_digest": prompt_digest,
        "schema_digest": schema_digest,
        "context_digest": context_digest,
    }
    return sha256_bytes(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _codex_failure_reason(output: bytes, *, exit_code: int | None) -> str:
    """Extract the bounded API/CLI reason from a Codex JSONL transcript."""
    candidates: list[str] = []
    for raw_line in output.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get("message")
        if isinstance(message, str):
            candidates.append(message)
        error = event.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            candidates.append(error["message"])
    for candidate in reversed(candidates):
        try:
            nested = json.loads(candidate)
        except json.JSONDecodeError:
            return candidate[:2000]
        if isinstance(nested, dict) and isinstance(nested.get("error"), dict):
            error = nested["error"]
            code = error.get("code")
            message = error.get("message")
            if isinstance(message, str):
                prefix = f"{code}: " if isinstance(code, str) and code else ""
                return (prefix + message)[:2000]
        return candidate[:2000]
    return f"codex exec exited with code {exit_code}"


def _owner_root(root: Path, change_id: str) -> tuple[Path, str]:
    candidate = root.resolve()
    if (
        (candidate / ".dls" / "config.toml").is_file()
        and StateStore(candidate).path(change_id).is_file()
    ):
        return candidate, "current-checkout"
    return resolve_registered_worktree(candidate, change_id), "registered-worktree"


def _pack_for_review(
    owner: Path,
    state: dict[str, Any],
    review_id: str,
) -> tuple[str, dict[str, Any]]:
    entry = next(
        (
            item
            for item in state["reviews"]
            if item.get("kind") == "pack" and item.get("review_id") == review_id
        ),
        None,
    )
    if not entry or not isinstance(entry.get("pack_path"), str):
        raise IntegrityError(f"ReviewPack is missing from state: {review_id}")
    relative = entry["pack_path"]
    pack = read_json(safe_resolve(owner, relative, must_exist=True))
    return relative, pack


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _lane_summary(owner: Path, entry: dict[str, Any]) -> dict[str, Any]:
    usage = entry.get("usage")
    if not isinstance(usage, dict):
        transcript = entry.get("transcript_path")
        if isinstance(transcript, str):
            path = safe_resolve(owner, transcript)
            if path.is_file():
                usage = _codex_usage_from_output(path.read_bytes())
    return {
        key: entry.get(key)
        for key in (
            "status",
            "kind",
            "model",
            "reasoning_effort",
            "attempt_ordinal",
            "started_at",
            "completed_at",
            "duration_seconds",
            "failure_reason",
        )
    } | {"usage": usage if isinstance(usage, dict) else None}


def _progress_summary(
    owner: Path,
    *,
    pack: dict[str, Any] | None,
    latest_by_lane: dict[str, dict[str, Any]],
    pipeline: dict[str, Any] | None,
    status_value: str,
) -> dict[str, Any]:
    summaries = {
        lane_key: _lane_summary(owner, entry)
        for lane_key, entry in latest_by_lane.items()
    }
    running_lane = next(
        (
            lane_key
            for lane_key, item in latest_by_lane.items()
            if item.get("status") == "running"
        ),
        None,
    )
    projected = 0
    if pack is not None:
        projected = int("native-diff" in pack.get("required_lanes", []))
        projected += len(pack.get("risk_lenses", []))
        projected += 2  # independent semantic and reconciliation
        if pack.get("review_mode") == "remediation":
            projected += 1  # conditional final-full
    projected = max(projected, len(latest_by_lane))
    completed = sum(
        item.get("status") == "completed" for item in latest_by_lane.values()
    )
    timestamps = [
        parsed
        for item in latest_by_lane.values()
        for parsed in (
            _parse_timestamp(item.get("started_at")),
            _parse_timestamp(item.get("completed_at")),
        )
        if parsed is not None
    ]
    if pipeline is not None:
        for field in ("started_at", "completed_at", "updated_at"):
            parsed = _parse_timestamp(pipeline.get(field))
            if parsed is not None:
                timestamps.append(parsed)
    started_at = min(timestamps) if timestamps else None
    terminal_at = max(timestamps) if timestamps else None
    end = (
        datetime.now(timezone.utc)
        if status_value == "running"
        else terminal_at
    )
    elapsed = (
        max(0.0, (end - started_at).total_seconds())
        if started_at is not None and end is not None
        else None
    )
    usage_totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    usage_seen = False
    for summary in summaries.values():
        usage = summary.get("usage")
        if not isinstance(usage, dict):
            continue
        usage_seen = True
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            usage_totals[key] += int(usage.get(key, 0))
    usage_totals["uncached_input_tokens"] = max(
        0,
        usage_totals["input_tokens"] - usage_totals["cached_input_tokens"],
    )
    context_totals = None
    for item in reversed(list(latest_by_lane.values())):
        context_path = item.get("context_manifest_path")
        if not isinstance(context_path, str) or "context/" not in context_path:
            continue
        path = safe_resolve(owner, context_path)
        if path.is_file():
            manifest = read_json(path)
            if isinstance(manifest.get("totals"), dict):
                context_totals = manifest["totals"]
                break
    return {
        "stage": (
            pipeline.get("stage")
            if pipeline is not None
            else running_lane
            or ("finalize" if status_value == "failed-finalize" else status_value)
        ),
        "active_lane": running_lane,
        "completed_lanes": completed,
        "projected_lanes": projected,
        "elapsed_seconds": elapsed,
        "last_transition_at": (
            terminal_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if terminal_at is not None
            else None
        ),
        "usage": usage_totals if usage_seen else None,
        "context": context_totals,
        "lanes": summaries,
    }


def _terminal_lane_next_action(
    terminal: dict[str, Any],
    lane_attempts: list[dict[str, Any]],
) -> dict[str, str]:
    lane_key = terminal.get("lane_key")
    current_schema_digest: str | None = None
    if isinstance(lane_key, str) and lane_key.startswith("specialist:"):
        current_schema_digest = sha256_file(
            SCHEMAS_ROOT / "specialist-decision.schema.json"
        )
    elif lane_key in {
        "semantic:full",
        "semantic:targeted",
        "semantic:final-full",
        "reconciliation",
    }:
        current_schema_digest = sha256_file(
            SCHEMAS_ROOT / "review-decision.schema.json"
        )
    if (
        current_schema_digest is not None
        and terminal.get("schema_digest") != current_schema_digest
    ):
        return {
            "id": "retry-review",
            "detail": "the installed lane contract changed after the failed attempt",
        }
    contract_digest = terminal.get("lane_contract_digest")
    matching = [
        item
        for item in lane_attempts
        if item.get("lane_key") == lane_key
        and (
            item.get("lane_contract_digest") == contract_digest
            if isinstance(contract_digest, str) and contract_digest
            else item.get("schema_digest") == terminal.get("schema_digest")
        )
    ]
    if (
        terminal.get("status") in RETRYABLE_REVIEW_LANE_STATUSES
        and len(matching) < REVIEW_LANE_MAX_ATTEMPTS
    ):
        return {
            "id": "retry-review",
            "detail": f"last_status={terminal.get('status')}",
        }
    return {
        "id": "inspect-review-failure",
        "detail": terminal.get("failure_reason")
        or f"last_status={terminal.get('status')}; automatic retry is not safe",
    }


def review_status(
    root: Path,
    *,
    change_id: str,
    review_id: str | None = None,
    verbose: bool = False,
) -> dict[str, Any]:
    owner, owner_selection = _owner_root(root, change_id)
    state = StateStore(owner).load(change_id)
    current_head = git_head(owner)
    result_entry = next(
        (
            item
            for item in reversed(state["reviews"])
            if item.get("kind") == "result"
            and (review_id is None or item.get("review_id") == review_id)
            and (review_id is not None or item.get("head_sha") == current_head)
        ),
        None,
    )
    pack_entry = next(
        (
            item
            for item in reversed(state["reviews"])
            if item.get("kind") == "pack"
            and (review_id is None or item.get("review_id") == review_id)
            and (review_id is not None or item.get("head_sha") == current_head)
        ),
        None,
    )
    selected_review_id = review_id
    status_result_entry = result_entry
    if result_entry:
        selected_review_id = result_entry["review_id"]
    elif pack_entry:
        selected_review_id = pack_entry["review_id"]
    lane_attempts = [
        item
        for item in state["reviews"]
        if isinstance(item, dict)
        and item.get("review_id") == selected_review_id
        and isinstance(item.get("lane_key"), str)
    ]
    latest_by_lane: dict[str, dict[str, Any]] = {}
    for attempt in lane_attempts:
        latest_by_lane[attempt["lane_key"]] = attempt
    pipelines = [
        item
        for item in state["reviews"]
        if isinstance(item, dict)
        and item.get("kind") == "pipeline"
        and item.get("review_id") == selected_review_id
    ]
    pipeline = max(
        pipelines,
        key=lambda item: item.get("updated_at") or item.get("started_at") or "",
        default=None,
    )
    terminal_lane = next(
        (
            item
            for item in reversed(list(latest_by_lane.values()))
            if item.get("status") not in {"completed", "running"}
        ),
        None,
    )
    pipeline_alive = bool(
        pipeline is not None
        and pipeline.get("status") == "running"
        and _process_is_alive(pipeline.get("runner_pid"))
    )
    remediation_manifest_path: str | None = None
    if result_entry:
        status_value = "completed"
        next_action = {"id": "review-complete", "detail": result_entry["result_path"]}
    elif terminal_lane is not None:
        status_value = "failed"
        next_action = _terminal_lane_next_action(terminal_lane, lane_attempts)
    elif any(item.get("status") == "running" for item in latest_by_lane.values()) or pipeline_alive:
        status_value = "running"
        next_action = {"id": "wait-review", "detail": "review pipeline is active"}
    elif pipeline is not None and pipeline.get("status") in {
        "failed",
        "failed-finalize",
    }:
        status_value = pipeline["status"]
        next_action = {
            "id": "resume-review" if status_value == "failed-finalize" else "retry-review",
            "detail": pipeline.get("failure_reason") or pipeline.get("stage") or "review failed",
        }
    elif pipeline is not None and pipeline.get("status") == "running":
        status_value = "failed"
        next_action = {
            "id": "resume-review",
            "detail": "review pipeline owner exited before canonical completion",
        }
    elif pack_entry:
        completed_without_result = bool(latest_by_lane) and all(
            item.get("status") == "completed" for item in latest_by_lane.values()
        ) and "reconciliation" in latest_by_lane
        if completed_without_result:
            status_value = "failed-finalize"
            next_action = {
                "id": "resume-review",
                "detail": "all model lanes completed but ReviewIR was not imported",
            }
        else:
            status_value = "ready"
            next_action = {"id": "start-review", "detail": pack_entry["pack_path"]}
    else:
        latest_result = _latest_review_result(state)
        if latest_result is None or review_id is not None:
            status_value = "not-prepared"
            next_action = {
                "id": "provide-review-base",
                "detail": "no exact-HEAD ReviewPack or imported result",
            }
        else:
            selected_review_id = latest_result.get("review_id")
            status_result_entry = latest_result
            if isinstance(selected_review_id, str):
                remediation_manifest_path = _existing_remediation_manifest_path(
                    owner,
                    change_id=change_id,
                    review_entry=latest_result,
                    review_id=selected_review_id,
                )
            readiness = review_ready(
                owner,
                change_id=change_id,
                base_ref=None,
                expected_revision=state["state_revision"],
                operation_id="review-status-projection",
                dry_run=True,
            )
            status_value = "ready" if readiness["ok"] else "blocked"
            next_action = readiness["next_action"]
    runner_contract = "legacy-provenance"
    provenance_pack_entry = pack_entry
    if provenance_pack_entry is None and isinstance(selected_review_id, str):
        provenance_pack_entry = next(
            (
                item
                for item in reversed(state["reviews"])
                if item.get("kind") == "pack"
                and item.get("review_id") == selected_review_id
            ),
            None,
        )
    pack: dict[str, Any] | None = None
    if provenance_pack_entry and isinstance(provenance_pack_entry.get("pack_path"), str):
        pack = read_json(
            safe_resolve(owner, provenance_pack_entry["pack_path"], must_exist=True)
        )
        runner_contract = pack.get("runner_contract", runner_contract)
    presentation = None
    if result_entry is not None:
        _, report = _read_review_result(owner, result_entry)
        presentation = build_review_presentation(owner, report)
    progress = _progress_summary(
        owner,
        pack=pack,
        latest_by_lane=latest_by_lane,
        pipeline=pipeline,
        status_value=status_value,
    )
    payload = {
        "ok": True,
        "changed": False,
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "current_head": current_head,
        "review_id": selected_review_id,
        "status": status_value,
        "runner_contract": runner_contract,
        "progress": progress,
        "lanes": progress["lanes"],
        "verdict": status_result_entry.get("verdict") if status_result_entry else None,
        "review_result_path": (
            status_result_entry.get("result_path") if status_result_entry else None
        ),
        "remediation_manifest_path": (
            (status_result_entry.get("remediation_manifest_path") if status_result_entry else None)
            or remediation_manifest_path
        ),
        "presentation": presentation,
        "next_action": next_action,
    }
    if verbose:
        payload["lane_details"] = latest_by_lane
        payload["pipeline"] = pipeline
    return payload


def _render_prompt(name: str, values: dict[str, str]) -> str:
    path = REVIEW_PROMPTS_ROOT / name
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace(f"{{{{{key}}}}}", value)
    if "{{" in text or "}}" in text:
        raise IntegrityError(f"Unresolved review prompt placeholder: {name}")
    return text


def _model_argv(
    *,
    model: str,
    effort: str,
    output_schema: str,
    output_path: str,
) -> list[str]:
    return [
        "codex",
        "exec",
        "--strict-config",
        "--model",
        model,
        "-c",
        f'model_reasoning_effort="{effort}"',
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--json",
        "--color",
        "never",
        "--output-schema",
        output_schema,
        "--output-last-message",
        output_path,
        (
            "Read and follow .dls-review-input/prompt.md. "
            "Return only the requested JSON object."
        ),
    ]


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _prepare_isolated_workspace(
    owner: Path,
    *,
    pack: dict[str, Any],
    context_path: str,
    prompt_text: str,
    schema_path: Path,
    extra_files: dict[str, Path | bytes],
) -> tuple[Path, Path]:
    temporary_parent = Path(tempfile.mkdtemp(prefix="dls-review-"))
    workspace = temporary_parent / "checkout"
    try:
        run_git(
            owner,
            "worktree",
            "add",
            "--detach",
            str(workspace),
            pack["head_sha"],
        )
        context_source = safe_resolve(owner, context_path, must_exist=True)
        manifest = read_json(context_source)
        for item in manifest.get("inputs", []):
            relative = item.get("path")
            if not isinstance(relative, str):
                raise IntegrityError("Review context contains an invalid input path")
            source = safe_resolve(owner, relative, must_exist=True)
            expected_digest = item.get("sha256")
            if (
                isinstance(expected_digest, str)
                and sha256_file(source) != expected_digest
            ):
                raise IntegrityError(
                    f"Review context input digest mismatch: {relative}"
                )
            _copy_file(source, workspace / relative)
        input_root = workspace / ".dls-review-input"
        _copy_file(context_source, input_root / "context.json")
        _copy_file(schema_path, input_root / "output.schema.json")
        atomic_write_text(input_root / "prompt.md", prompt_text, backup=False)
        for relative, value in extra_files.items():
            destination = input_root / relative
            if isinstance(value, bytes):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(value)
            else:
                _copy_file(value, destination)
        return workspace, temporary_parent
    except Exception:
        run_git(owner, "worktree", "remove", "--force", str(workspace), check=False)
        shutil.rmtree(temporary_parent, ignore_errors=True)
        raise


def _cleanup_isolated_workspace(
    owner: Path,
    workspace: Path,
    temporary_parent: Path,
) -> None:
    run_git(owner, "worktree", "remove", "--force", str(workspace), check=False)
    shutil.rmtree(temporary_parent, ignore_errors=True)
    run_git(owner, "worktree", "prune", check=False)


def _validate_structured_payload(
    payload: dict[str, Any],
    *,
    payload_kind: str,
    lens_id: str | None,
) -> None:
    if payload_kind == "specialist":
        if payload.get("lens_id") != lens_id:
            raise IntegrityError("Specialist output lens_id mismatch")
        if not isinstance(payload.get("findings"), list):
            raise IntegrityError("Specialist output requires findings")
        return
    required = {
        "verdict",
        "summary",
        "findings",
        "prior_finding_verdicts",
    }
    missing = sorted(required - payload.keys())
    if missing:
        raise IntegrityError(
            "Semantic decision missing fields: " + ", ".join(missing)
        )
    if payload["verdict"] not in {"review-clear", "not-clear", "blocked"}:
        raise IntegrityError("Semantic decision verdict is invalid")
    for field in ("findings", "prior_finding_verdicts"):
        if not isinstance(payload[field], list):
            raise IntegrityError(f"Semantic decision {field} must be an array")
    if "ticket_verdicts" in payload and not isinstance(
        payload["ticket_verdicts"],
        list,
    ):
        raise IntegrityError("Semantic decision ticket_verdicts must be an array")


def _completed_lane_payload(
    owner: Path,
    entry: dict[str, Any],
    *,
    payload_kind: str,
    lens_id: str | None,
) -> dict[str, Any]:
    output_path = entry.get("output_path")
    if not isinstance(output_path, str):
        raise IntegrityError("Completed review lane is missing output_path")
    path = safe_resolve(owner, output_path, must_exist=True)
    if sha256_file(path) != entry.get("output_digest"):
        raise IntegrityError("Completed review lane output digest mismatch")
    payload = read_json(path)
    _validate_structured_payload(
        payload,
        payload_kind=payload_kind,
        lens_id=lens_id,
    )
    return payload


def _mark_lane_pipeline_failed(
    owner: Path,
    *,
    change_id: str,
    review_id: str,
    operation_id: str,
    lane_key: str,
    reason: str,
) -> None:
    _update_pipeline(
        owner,
        change_id=change_id,
        review_id=review_id,
        operation_id=operation_id,
        stage=lane_key,
        status="failed",
        failure_reason=reason,
    )


def _execute_structured_lane(
    owner: Path,
    *,
    change_id: str,
    pack: dict[str, Any],
    context_path: str,
    root_operation_id: str,
    lane_key: str,
    lane_kind: str,
    model: str,
    effort: str,
    prompt_text: str,
    schema_path: Path,
    payload_kind: str,
    lens_id: str | None = None,
    extra_files: dict[str, Path | bytes] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    state_store = StateStore(owner)
    safe_lane = lane_key.replace(":", "-")
    prompt_digest = sha256_bytes(prompt_text.encode("utf-8"))
    schema = read_json(schema_path)
    try:
        _validate_strict_output_schema(schema)
    except IntegrityError as exc:
        _mark_lane_pipeline_failed(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
            operation_id=root_operation_id,
            lane_key=lane_key,
            reason=str(exc),
        )
        raise
    schema_digest = sha256_file(schema_path)
    context_source = safe_resolve(owner, context_path, must_exist=True)
    context_digest = sha256_file(context_source)
    lane_contract_digest = _lane_contract_digest(
        pack=pack,
        lane_key=lane_key,
        model=model,
        effort=effort,
        prompt_digest=prompt_digest,
        schema_digest=schema_digest,
        context_digest=context_digest,
    )
    while True:
        state = state_store.load(change_id)
        attempts = _review_lane_entries(
            state,
            review_id=pack["review_id"],
            lane_key=lane_key,
        )
        contract_attempts = [
            item
            for item in attempts
            if item.get("lane_contract_digest") == lane_contract_digest
        ]
        running = next(
            (
                item
                for item in reversed(attempts)
                if item.get("status") == "running"
            ),
            None,
        )
        if running:
            if _process_is_alive(running.get("runner_pid")) and not _attempt_lease_expired(
                running
            ):
                return running, None
            state_store.finish_review_lane(
                change_id,
                attempt_id=running["attempt_id"],
                expected_status="running",
                updates={
                    "status": "abandoned",
                    "completed_at": utc_now(),
                    "failure_reason": "runner process disappeared or lease expired",
                },
            )
            continue
        completed = next(
            (
                item
                for item in reversed(contract_attempts)
                if item.get("status") == "completed"
            ),
            None,
        )
        if completed:
            return completed, _completed_lane_payload(
                owner,
                completed,
                payload_kind=payload_kind,
                lens_id=lens_id,
            )
        terminal = contract_attempts[-1] if contract_attempts else None
        if terminal and terminal.get("status") not in RETRYABLE_REVIEW_LANE_STATUSES:
            reason = terminal.get("failure_reason") or (
                f"Review lane {lane_key} failed: status={terminal.get('status')}"
            )
            _mark_lane_pipeline_failed(
                owner,
                change_id=change_id,
                review_id=pack["review_id"],
                operation_id=root_operation_id,
                lane_key=lane_key,
                reason=reason,
            )
            raise IntegrityError(
                f"Review lane {lane_key} failed: status={terminal.get('status')}; "
                f"reason={reason}"
            )
        if len(contract_attempts) >= REVIEW_LANE_MAX_ATTEMPTS:
            reason = f"Review lane {lane_key} exhausted automatic attempts"
            _mark_lane_pipeline_failed(
                owner,
                change_id=change_id,
                review_id=pack["review_id"],
                operation_id=root_operation_id,
                lane_key=lane_key,
                reason=reason,
            )
            raise IntegrityError(reason)
        ordinal = max(
            (
                item.get("attempt_ordinal", 0)
                for item in attempts
                if isinstance(item.get("attempt_ordinal", 0), int)
            ),
            default=0,
        ) + 1
        operation_id = (
            f"{root_operation_id}:{safe_lane}"
            if ordinal == 1
            else f"{root_operation_id}:{safe_lane}:retry-{ordinal}"
        )
        attempt_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                (
                    f"dls:{change_id}:{pack['review_id']}:{lane_key}:"
                    f"{ordinal}:{operation_id}"
                ),
            )
        )
        cache_root = (
            f".dls/cache/reviews/{change_id}/{pack['review_id']}"
        )
        output_relative = f"{cache_root}/{safe_lane}-{attempt_id}.json"
        transcript_relative = (
            f"{cache_root}/{safe_lane}-transcript-{attempt_id}.jsonl"
        )
        prompt_relative = f"{cache_root}/{safe_lane}-prompt-{attempt_id}.md"
        normalized_argv = _model_argv(
            model=model,
            effort=effort,
            output_schema=".dls-review-input/output.schema.json",
            output_path=".dls-review-output.json",
        )
        snapshot_before = git_source_snapshot_digest(owner)
        proposed = {
            "review_id": pack["review_id"],
            "kind": lane_kind,
            "lane_key": lane_key,
            "attempt_id": attempt_id,
            "attempt_ordinal": ordinal,
            "operation_id": operation_id,
            "runner_pid": os.getpid(),
            "runner_contract": REVIEW_RUNNER_CONTRACT,
            "lane_contract_digest": lane_contract_digest,
            "status": "running",
            "base_sha": pack.get("comparison_base_sha", pack["base_sha"]),
            "head_sha": pack["head_sha"],
            "pack_digest": pack["pack_digest"],
            "model": model,
            "reasoning_effort": effort,
            "argv": normalized_argv,
            "prompt_path": prompt_relative,
            "prompt_digest": prompt_digest,
            "schema_path": str(schema_path.relative_to(PLUGIN_ROOT)),
            "schema_digest": schema_digest,
            "context_manifest_path": context_path,
            "context_digest": context_digest,
            "output_path": output_relative,
            "transcript_path": transcript_relative,
            "source_snapshot_before": snapshot_before,
            "started_at": utc_now(),
        }
        state, claimed_attempt, claimed = state_store.claim_review_lane(
            change_id,
            attempt=proposed,
            operation_kind=f"review-run:{lane_key}",
            max_attempts=REVIEW_LANE_MAX_ATTEMPTS,
        )
        if not claimed:
            if claimed_attempt.get("status") == "running":
                return claimed_attempt, None
            continue
        output_path = safe_resolve(owner, output_relative)
        transcript_path = safe_resolve(owner, transcript_relative)
        prompt_path = safe_resolve(owner, prompt_relative)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(prompt_path, prompt_text, backup=False)
        workspace: Path | None = None
        temporary_parent: Path | None = None
        payload: dict[str, Any] | None = None
        recorded: dict[str, Any] | None = None
        try:
            workspace, temporary_parent = _prepare_isolated_workspace(
                owner,
                pack=pack,
                context_path=context_path,
                prompt_text=prompt_text,
                schema_path=schema_path,
                extra_files=extra_files or {},
            )
            execution = _run_bounded_command(
                normalized_argv,
                cwd=workspace,
                environment=allowed_environment(["HOME", "CODEX_HOME"]),
                timeout_seconds=NATIVE_REVIEW_TIMEOUT_SECONDS,
                max_output_bytes=NATIVE_REVIEW_TRANSCRIPT_MAX_BYTES,
                terminate_on_overflow=False,
            )
            atomic_write_text(
                transcript_path,
                execution["output"].decode("utf-8", errors="replace"),
                backup=False,
            )
            temporary_output = workspace / ".dls-review-output.json"
            output_exists = temporary_output.is_file()
            output_bytes = temporary_output.stat().st_size if output_exists else 0
            output_overflow = output_bytes > NATIVE_REVIEW_MAX_OUTPUT_BYTES
            if output_exists:
                raw_output = temporary_output.read_bytes()[
                    :NATIVE_REVIEW_MAX_OUTPUT_BYTES
                ]
                atomic_write_text(
                    output_path,
                    raw_output.decode("utf-8", errors="replace"),
                    backup=False,
                )
            status_value = "completed"
            failure_reason: str | None = None
            if execution["timed_out"]:
                status_value = "timeout"
            elif execution["exit_code"] != 0:
                status_value = "failed"
                failure_reason = _codex_failure_reason(
                    execution["output"],
                    exit_code=execution["exit_code"],
                )
            elif not output_exists or output_bytes == 0:
                status_value = "missing-output"
            elif output_overflow:
                status_value = "output-cap"
            else:
                try:
                    payload = read_json(output_path)
                    _validate_structured_payload(
                        payload,
                        payload_kind=payload_kind,
                        lens_id=lens_id,
                    )
                except IntegrityError as exc:
                    status_value = "invalid-output"
                    failure_reason = str(exc)
            snapshot_after = git_source_snapshot_digest(owner)
            if status_value == "completed" and snapshot_after != snapshot_before:
                status_value = "source-changed"
            final_updates = {
                "status": status_value,
                "output_path": output_relative if output_path.is_file() else None,
                "output_digest": (
                    sha256_file(output_path) if output_path.is_file() else None
                ),
                "output_bytes": output_bytes,
                "exit_code": execution["exit_code"],
                "timed_out": execution["timed_out"],
                "overflow": output_overflow,
                "transcript_path": transcript_relative,
                "transcript_digest": sha256_file(transcript_path),
                "transcript_output_bytes": execution["output_bytes"],
                "transcript_retained_bytes": len(execution["output"]),
                "transcript_truncated": execution["overflow"],
                "usage": _codex_usage_from_output(execution["output"]),
                "duration_seconds": execution["duration_seconds"],
                "source_snapshot_digest": snapshot_after,
                "failure_reason": failure_reason,
                "completed_at": utc_now(),
            }
            _, recorded, _ = state_store.finish_review_lane(
                change_id,
                attempt_id=attempt_id,
                expected_status="running",
                updates=final_updates,
            )
        except Exception as exc:
            failure_status = (
                "failed" if isinstance(exc, IntegrityError) else "abandoned"
            )
            failure_text = f"{type(exc).__name__}: {exc}\n"
            if not transcript_path.exists():
                atomic_write_text(transcript_path, failure_text, backup=False)
            snapshot_after = git_source_snapshot_digest(owner)
            _, recorded, _ = state_store.finish_review_lane(
                change_id,
                attempt_id=attempt_id,
                expected_status="running",
                updates={
                    "status": failure_status,
                    "output_path": output_relative if output_path.is_file() else None,
                    "output_digest": (
                        sha256_file(output_path) if output_path.is_file() else None
                    ),
                    "transcript_path": transcript_relative,
                    "transcript_digest": sha256_file(transcript_path),
                    "source_snapshot_digest": snapshot_after,
                    "failure_reason": str(exc),
                    "completed_at": utc_now(),
                },
            )
        finally:
            if workspace is not None and temporary_parent is not None:
                _cleanup_isolated_workspace(
                    owner,
                    workspace,
                    temporary_parent,
                )
        assert recorded is not None
        if recorded["status"] == "completed":
            assert payload is not None
            return recorded, payload
        if recorded["status"] in RETRYABLE_REVIEW_LANE_STATUSES:
            continue
        reason = recorded.get("failure_reason") or (
            f"Review lane {lane_key} failed: status={recorded['status']}"
        )
        _mark_lane_pipeline_failed(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
            operation_id=root_operation_id,
            lane_key=lane_key,
            reason=reason,
        )
        raise IntegrityError(
            f"Review lane {lane_key} failed: status={recorded['status']}; "
            f"reason={reason}"
        )


def _lane_provenance(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        key: entry.get(key)
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
            "context_manifest_path",
            "context_digest",
            "output_path",
            "output_digest",
            "transcript_path",
            "transcript_digest",
            "source_snapshot_digest",
        )
    }


def _has_review_blocker(decision: dict[str, Any]) -> bool:
    return any(
        finding.get("severity") == "blocker"
        and "review" in _finding_blocks(finding)
        for finding in decision.get("findings", [])
        if isinstance(finding, dict)
    )


def _derive_ticket_verdicts(
    pack: dict[str, Any],
    findings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Derive stage-correct ticket verdicts from canonical findings.

    Model-produced ticket verdicts are advisory input at most. Review readiness
    is a mechanical relation between ticket links, severity, and ``blocks``;
    deriving it here prevents a release-only note from making code review
    ``not-clear`` and avoids paying for a correction model call.
    """
    verdicts: list[dict[str, Any]] = []
    for ticket_id in pack["tickets"]:
        linked = [
            finding
            for finding in findings
            if ticket_id in finding.get("ticket_ids", [])
        ]
        review_blockers = [
            finding
            for finding in linked
            if finding.get("severity") in {"blocker", "should-fix"}
            and "review" in _finding_blocks(finding)
        ]
        if any(finding.get("kind") == "external" for finding in review_blockers):
            verdict = "blocked"
        elif review_blockers:
            verdict = "not-clear"
        else:
            verdict = "clear"
        verdicts.append(
            {
                "ticket_id": ticket_id,
                "verdict": verdict,
                "finding_ids": [finding["id"] for finding in linked],
            }
        )
    return verdicts


def _derive_review_verdict(
    ticket_verdicts: list[dict[str, Any]],
    findings: list[dict[str, Any]] | None = None,
) -> str:
    review_blockers = [
        finding
        for finding in findings or []
        if finding.get("severity") in {"blocker", "should-fix"}
        and "review" in _finding_blocks(finding)
    ]
    if any(finding.get("kind") == "external" for finding in review_blockers):
        return "blocked"
    values = {item["verdict"] for item in ticket_verdicts}
    if "blocked" in values:
        return "blocked"
    if review_blockers or "not-clear" in values:
        return "not-clear"
    return "review-clear"


def _update_pipeline(
    owner: Path,
    *,
    change_id: str,
    review_id: str,
    operation_id: str,
    stage: str,
    status: str = "running",
    create: bool = False,
    failure_reason: str | None = None,
) -> None:
    updates: dict[str, Any] = {
        "status": status,
        "stage": stage,
        "runner_pid": os.getpid(),
        "updated_at": utc_now(),
    }
    if failure_reason is not None:
        updates["failure_reason"] = failure_reason
    if status in {"completed", "failed", "failed-finalize"}:
        updates["completed_at"] = utc_now()
    StateStore(owner).update_review_pipeline(
        change_id,
        review_id=review_id,
        operation_id=operation_id,
        updates=updates,
        create=create,
    )


def _build_review_ir(
    *,
    pack: dict[str, Any],
    start_result: dict[str, Any],
    decision: dict[str, Any],
    independent_entry: dict[str, Any],
    reconciliation_entry: dict[str, Any],
    specialist_entries: list[tuple[dict[str, Any], str]],
    final_full_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    for raw in decision["findings"]:
        finding = dict(raw)
        finding["base_sha"] = pack["base_sha"]
        finding["head_sha"] = pack["head_sha"]
        findings.append(finding)
    ticket_verdicts = _derive_ticket_verdicts(pack, findings)
    review_verdict = _derive_review_verdict(ticket_verdicts, findings)
    pass_kind = "full" if pack["review_mode"] == "full" else "targeted"
    passes = [
        {
            "kind": pass_kind,
            "status": "completed",
            "attempt_id": independent_entry["attempt_id"],
            "operation_id": independent_entry["operation_id"],
            "draft_path": independent_entry["output_path"],
            "draft_digest": independent_entry["output_digest"],
            "transcript_path": independent_entry["transcript_path"],
            "transcript_digest": independent_entry["transcript_digest"],
        }
    ]
    if final_full_entry is not None:
        passes.append(
            {
                "kind": "final-full",
                "status": "completed",
                "attempt_id": final_full_entry["attempt_id"],
                "operation_id": final_full_entry["operation_id"],
                "draft_path": final_full_entry["output_path"],
                "draft_digest": final_full_entry["output_digest"],
                "transcript_path": final_full_entry["transcript_path"],
                "transcript_digest": final_full_entry["transcript_digest"],
            }
        )
    lanes: dict[str, Any] = {
        "semantic": {
            "status": "completed",
            "model": independent_entry["model"],
            "reasoning_effort": independent_entry["reasoning_effort"],
            "context_manifest_path": start_result["review_context_path"],
            "context_manifest_digest": start_result["review_context_digest"],
            "independent_draft_path": independent_entry["output_path"],
            "independent_draft_digest": independent_entry["output_digest"],
            "attempt_id": independent_entry["attempt_id"],
            "operation_id": independent_entry["operation_id"],
            "transcript_path": independent_entry["transcript_path"],
            "transcript_digest": independent_entry["transcript_digest"],
            "passes": passes,
        },
        "reconciliation": _lane_provenance(reconciliation_entry),
        "specialists": [
            {
                "lens_id": lens_id,
                **_lane_provenance(entry),
                "draft_path": entry["output_path"],
                "draft_digest": entry["output_digest"],
            }
            for entry, lens_id in specialist_entries
        ],
    }
    native = start_result.get("native")
    if native:
        lanes["native"] = {
            "status": "completed",
            "attempt_id": native["attempt_id"],
            "operation_id": native.get("operation_id"),
            "model": native["model"],
            "reasoning_effort": native["reasoning_effort"],
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
    return {
        "schema_version": 2,
        "runner_contract": REVIEW_RUNNER_CONTRACT,
        "review_id": pack["review_id"],
        "change_id": pack["change_id"],
        "base_sha": pack["base_sha"],
        "epic_base_sha": pack["epic_base_sha"],
        "comparison_base_sha": pack["comparison_base_sha"],
        "head_sha": pack["head_sha"],
        "pack_digest": pack["pack_digest"],
        "definition_digest": pack["definition_digest"],
        "review_mode": pack["review_mode"],
        "verdict": review_verdict,
        "summary": decision["summary"],
        "lanes": lanes,
        "ticket_verdicts": ticket_verdicts,
        "prior_finding_verdicts": decision["prior_finding_verdicts"],
        "findings": findings,
    }


def review_run(
    root: Path,
    *,
    change_id: str,
    pack_path: str | None,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    effective_operation_id = operation_id or str(uuid.uuid4())
    explicit_pack = Path(pack_path).is_absolute() if pack_path else False
    if not explicit_pack:
        existing_status = review_status(root, change_id=change_id)
        if (
            existing_status["status"] == "completed"
            and existing_status["review_result_path"]
        ):
            existing_status.update(
                {
                    "dry_run": False,
                    "operation_id": effective_operation_id,
                    "review_pack_path": None,
                    "pack_created": False,
                }
            )
            return existing_status
    started = review_start(
        root,
        change_id=change_id,
        pack_path=pack_path,
        operation_id=f"{effective_operation_id}:native",
        dry_run=dry_run,
    )
    if not started.get("ok") or started.get("status") == "running":
        started["review_result_path"] = None
        return started
    if dry_run:
        started.update(
            {
                "status": "ready",
                "runner_contract": REVIEW_RUNNER_CONTRACT,
                "projected_lanes": {
                    "native": started["native_required"],
                    "specialists": [
                        item["id"] for item in started["risk_lenses"]
                    ],
                    "semantic": (
                        "full"
                        if started["review_mode"] == "full"
                        else "targeted"
                    ),
                    "reconciliation": True,
                    "final_full": (
                        "conditional"
                        if started["review_mode"] == "remediation"
                        else False
                    ),
                },
                "review_result_path": None,
                "next_action": {
                    "id": "start-review-run",
                    "detail": "end-to-end review pipeline is ready",
                },
            }
        )
        return started
    owner = Path(started["owner_root"])
    state = StateStore(owner).load(change_id)
    relative_pack_path, pack = _pack_for_review(
        owner,
        state,
        started["review_id"],
    )
    pipeline_operation_id = f"{effective_operation_id}:{pack['review_id']}"
    _validate_review_pack_current(owner, state=state, pack=pack)
    context_path = started["review_context_path"]
    semantic_effort = _semantic_review_effort(state)
    prompt_values = {
        "CHANGE_ID": change_id,
        "REVIEW_ID": pack["review_id"],
        "COMPARISON_BASE_SHA": pack["comparison_base_sha"],
        "EPIC_BASE_SHA": pack["epic_base_sha"],
        "HEAD_SHA": pack["head_sha"],
    }
    _update_pipeline(
        owner,
        change_id=change_id,
        review_id=pack["review_id"],
        operation_id=pipeline_operation_id,
        stage="specialists" if pack.get("risk_lenses") else "semantic-independent",
        create=True,
    )
    specialist_entries: list[tuple[dict[str, Any], str]] = []
    specialist_paths: dict[str, Path] = {}
    for lens in pack.get("risk_lenses", []):
        _update_pipeline(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
            operation_id=pipeline_operation_id,
            stage=f"specialist:{lens['id']}",
        )
        prompt = _render_prompt(
            "specialist.md",
            {
                **prompt_values,
                "LENS_ID": lens["id"],
                "LENS_FOCUS": lens["focus"],
            },
        )
        entry, payload = _execute_structured_lane(
            owner,
            change_id=change_id,
            pack=pack,
            context_path=context_path,
            root_operation_id=pipeline_operation_id,
            lane_key=f"specialist:{lens['id']}",
            lane_kind="specialist",
            model=SPECIALIST_MODEL,
            effort=SPECIALIST_EFFORT,
            prompt_text=prompt,
            schema_path=SCHEMAS_ROOT / "specialist-decision.schema.json",
            payload_kind="specialist",
            lens_id=lens["id"],
        )
        if payload is None:
            return {
                **review_status(
                    owner,
                    change_id=change_id,
                    review_id=pack["review_id"],
                ),
                "operation_id": effective_operation_id,
                "review_result_path": None,
            }
        specialist_entries.append((entry, lens["id"]))
        specialist_paths[f"{lens['id']}.json"] = safe_resolve(
            owner,
            entry["output_path"],
            must_exist=True,
        )
    independent_kind = (
        "full" if pack["review_mode"] == "full" else "targeted"
    )
    independent_prompt = _render_prompt(
        "semantic-independent.md",
        {
            **prompt_values,
            "PASS_KIND": independent_kind,
        },
    )
    _update_pipeline(
        owner,
        change_id=change_id,
        review_id=pack["review_id"],
        operation_id=pipeline_operation_id,
        stage=f"semantic:{independent_kind}",
    )
    independent_entry, independent_decision = _execute_structured_lane(
        owner,
        change_id=change_id,
        pack=pack,
        context_path=context_path,
        root_operation_id=pipeline_operation_id,
        lane_key=f"semantic:{independent_kind}",
        lane_kind="semantic",
        model=SEMANTIC_MODEL,
        effort=semantic_effort,
        prompt_text=independent_prompt,
        schema_path=SCHEMAS_ROOT / "review-decision.schema.json",
        payload_kind="decision",
    )
    if independent_decision is None:
        return {
            **review_status(
                owner,
                change_id=change_id,
                review_id=pack["review_id"],
            ),
            "operation_id": effective_operation_id,
            "review_result_path": None,
        }
    native = started.get("native")
    native_bytes = b"No native lane was required for this ReviewPack.\n"
    if native and isinstance(native.get("output_path"), str):
        native_bytes = safe_resolve(
            owner,
            native["output_path"],
            must_exist=True,
        ).read_bytes()
    reconciliation_prompt = _render_prompt("reconcile.md", prompt_values)
    _update_pipeline(
        owner,
        change_id=change_id,
        review_id=pack["review_id"],
        operation_id=pipeline_operation_id,
        stage="reconciliation",
    )
    reconciliation_entry, decision = _execute_structured_lane(
        owner,
        change_id=change_id,
        pack=pack,
        context_path=context_path,
        root_operation_id=pipeline_operation_id,
        lane_key="reconciliation",
        lane_kind="reconciliation",
        model=SEMANTIC_MODEL,
        effort=semantic_effort,
        prompt_text=reconciliation_prompt,
        schema_path=SCHEMAS_ROOT / "review-decision.schema.json",
        payload_kind="decision",
        extra_files={
            "native.txt": native_bytes,
            "semantic-independent.json": safe_resolve(
                owner,
                independent_entry["output_path"],
                must_exist=True,
            ),
            **{
                f"specialists/{name}": path
                for name, path in specialist_paths.items()
            },
        },
    )
    if decision is None:
        return {
            **review_status(
                owner,
                change_id=change_id,
                review_id=pack["review_id"],
            ),
            "operation_id": effective_operation_id,
            "review_result_path": None,
        }
    final_full_entry: dict[str, Any] | None = None
    if pack["review_mode"] == "remediation" and not _has_review_blocker(decision):
        final_prompt = _render_prompt("final-full.md", prompt_values)
        _update_pipeline(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
            operation_id=pipeline_operation_id,
            stage="semantic:final-full",
        )
        final_full_entry, final_decision = _execute_structured_lane(
            owner,
            change_id=change_id,
            pack=pack,
            context_path=context_path,
            root_operation_id=pipeline_operation_id,
            lane_key="semantic:final-full",
            lane_kind="semantic",
            model=SEMANTIC_MODEL,
            effort=semantic_effort,
            prompt_text=final_prompt,
            schema_path=SCHEMAS_ROOT / "review-decision.schema.json",
            payload_kind="decision",
            extra_files={
                "native.txt": native_bytes,
                "semantic-independent.json": safe_resolve(
                    owner,
                    independent_entry["output_path"],
                    must_exist=True,
                ),
                "targeted-decision.json": safe_resolve(
                    owner,
                    reconciliation_entry["output_path"],
                    must_exist=True,
                ),
                **{
                    f"specialists/{name}": path
                    for name, path in specialist_paths.items()
                },
            },
        )
        if final_decision is None:
            return {
                **review_status(
                    owner,
                    change_id=change_id,
                    review_id=pack["review_id"],
                ),
                "operation_id": effective_operation_id,
                "review_result_path": None,
            }
        decision = final_decision
    _update_pipeline(
        owner,
        change_id=change_id,
        review_id=pack["review_id"],
        operation_id=pipeline_operation_id,
        stage="finalizing",
    )
    try:
        report = _build_review_ir(
            pack=pack,
            start_result=started,
            decision=decision,
            independent_entry=independent_entry,
            reconciliation_entry=reconciliation_entry,
            specialist_entries=specialist_entries,
            final_full_entry=final_full_entry,
        )
        _validate_review_report(report, change_id, pack)
        report_relative = (
            f".dls/cache/reviews/{change_id}/{pack['review_id']}/reviewir.json"
        )
        atomic_write_json(
            safe_resolve(owner, report_relative),
            report,
            backup=False,
        )
        current_state = StateStore(owner).load(change_id)
        imported = review_import(
            owner,
            change_id=change_id,
            report_path=report_relative,
            expected_revision=current_state["state_revision"],
            operation_id=f"{pipeline_operation_id}:import",
        )
    except Exception as exc:
        _update_pipeline(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
            operation_id=pipeline_operation_id,
            stage="finalizing",
            status="failed-finalize",
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
        raise
    _update_pipeline(
        owner,
        change_id=change_id,
        review_id=pack["review_id"],
        operation_id=pipeline_operation_id,
        stage="completed",
        status="completed",
    )
    presentation = build_review_presentation(owner, report)
    return {
        "ok": imported["review_result_path"] is not None,
        "dry_run": False,
        "changed": imported["changed"],
        "status": "completed",
        "change_id": change_id,
        "state_revision": imported["state_revision"],
        "operation_id": effective_operation_id,
        "owner_root": str(owner),
        "owner_selection": started["owner_selection"],
        "review_id": pack["review_id"],
        "review_pack_path": relative_pack_path,
        "pack_created": started["pack_created"],
        "runner_contract": REVIEW_RUNNER_CONTRACT,
        "verdict": imported["verdict"],
        "finding_counts": imported["finding_counts"],
        "review_result_path": imported["review_result_path"],
        "remediation_manifest_path": imported.get(
            "remediation_manifest_path"
        ),
        "presentation": presentation,
        "next_action": imported["next_action"],
    }
