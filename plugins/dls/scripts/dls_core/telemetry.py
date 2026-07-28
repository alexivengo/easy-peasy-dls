"""Privacy-preserving review telemetry, delivery status, and cache retention."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import IntegrityError, LockError
from .io import FileLock, atomic_write_json, read_json, safe_resolve, utc_now
from .operations import _codex_usage_from_output
from .repo import git_head
from .state import StateStore
from .worktrees import resolve_registered_worktree

METRICS_CONTRACT = "dls-review-metrics/v1"
TASK_CONTEXT_CONTRACT = "dls-task-context/v1"
TELEMETRY_ROOT = ".dls/cache/telemetry"
RAW_RETENTION_DAYS = 14
COMPLETED_REVIEW_RETENTION = 2

_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

_TASK_CONTEXT_STATUSES = {"fresh", "continued", "reused", "unavailable"}
_TASK_CONTEXT_ROLES = {"implementation", "review"}


def unavailable_task_context(role: str) -> dict[str, Any]:
    if role not in _TASK_CONTEXT_ROLES:
        raise IntegrityError(f"Unsupported task-context role: {role}")
    return {
        "contract": TASK_CONTEXT_CONTRACT,
        "status": "unavailable",
        "role": role,
        "reuse_reason": None,
        "prior_cycle_count": 0,
        "recommendation": None,
    }


def task_cycle_ref(*, role: str, components: dict[str, Any]) -> str:
    if role not in _TASK_CONTEXT_ROLES:
        raise IntegrityError(f"Unsupported task-context role: {role}")
    encoded = json.dumps(
        {"contract": TASK_CONTEXT_CONTRACT, "role": role, "components": components},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _public_task_context(
    *,
    status: str,
    role: str,
    reuse_reason: str | None,
    prior_cycle_count: int,
) -> dict[str, Any]:
    if status not in _TASK_CONTEXT_STATUSES:
        raise IntegrityError(f"Unsupported task-context status: {status}")
    recommendation = "open-fresh-task" if status == "reused" else None
    return {
        "contract": TASK_CONTEXT_CONTRACT,
        "status": status,
        "role": role,
        "reuse_reason": reuse_reason,
        "prior_cycle_count": max(0, int(prior_cycle_count)),
        "recommendation": recommendation,
    }


def _task_binding_files(owner: Path, change_id: str) -> tuple[Path, list[Path]]:
    relative = Path(TELEMETRY_ROOT) / change_id / "tasks"
    unresolved = owner.resolve() / relative
    current = owner.resolve()
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise IntegrityError(
                f"DLS task telemetry path contains a symlink: {current}"
            )
    directory = safe_resolve(owner, relative)
    if not directory.exists():
        return directory, []
    files = sorted(directory.glob("*.json"))
    if any(path.is_symlink() for path in files):
        raise IntegrityError("DLS task telemetry contains a symlink")
    return directory, files


def bind_task_context(
    owner: Path,
    *,
    change_id: str,
    role: str,
    cycle_ref: str,
    operation_id: str,
    record: bool = True,
    allow_cross_role: bool = False,
) -> dict[str, Any]:
    """Classify one Codex task without exposing its raw identifier publicly."""
    if role not in _TASK_CONTEXT_ROLES:
        raise IntegrityError(f"Unsupported task-context role: {role}")
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        return unavailable_task_context(role)
    thread_ref = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
    directory, _ = _task_binding_files(owner, change_id)
    unresolved_lock = (
        owner.resolve() / TELEMETRY_ROOT / change_id / ".task-context.lock"
    )
    if unresolved_lock.is_symlink():
        raise IntegrityError(
            f"DLS task telemetry lock is a symlink: {unresolved_lock}"
        )
    lock_path = safe_resolve(owner, f"{TELEMETRY_ROOT}/{change_id}/.task-context.lock")
    lock: FileLock | None = None
    for attempt in range(101):
        try:
            lock = FileLock(lock_path)
            lock.__enter__()
            break
        except LockError:
            if attempt == 100:
                return unavailable_task_context(role)
            time.sleep(0.01)
    if lock is None:
        return unavailable_task_context(role)
    try:
        directory, files = _task_binding_files(owner, change_id)
        bindings: list[dict[str, Any]] = []
        try:
            for path in files:
                value = read_json(path)
                if not isinstance(value, dict):
                    return unavailable_task_context(role)
                bindings.append(value)
        except (IntegrityError, OSError, UnicodeError):
            return unavailable_task_context(role)
        same_binding = next(
            (
                item
                for item in bindings
                if item.get("thread_id") == thread_id
                and item.get("role") == role
                and item.get("cycle_ref") == cycle_ref
            ),
            None,
        )
        same_thread = [item for item in bindings if item.get("thread_id") == thread_id]
        prior_cycles = {
            (item.get("role"), item.get("cycle_ref"))
            for item in same_thread
            if isinstance(item.get("role"), str)
            and isinstance(item.get("cycle_ref"), str)
            and not (
                item.get("role") == role and item.get("cycle_ref") == cycle_ref
            )
        }
        if same_binding is not None:
            status = "continued"
            reuse_reason = None
        else:
            cross_role = any(item.get("role") != role for item in same_thread)
            same_role_other_cycle = any(
                item.get("role") == role and item.get("cycle_ref") != cycle_ref
                for item in same_thread
            )
            if cross_role and not allow_cross_role:
                status = "reused"
                reuse_reason = "cross-role"
            elif same_role_other_cycle:
                status = "reused"
                reuse_reason = "same-role-new-cycle"
            else:
                status = "fresh"
                reuse_reason = None
        public = _public_task_context(
            status=status,
            role=role,
            reuse_reason=reuse_reason,
            prior_cycle_count=len(prior_cycles),
        )
        if record and same_binding is None:
            relative = (
                f"{TELEMETRY_ROOT}/{change_id}/tasks/"
                f"{role}-{cycle_ref}-{thread_ref}.json"
            )
            atomic_write_json(
                safe_resolve(owner, relative),
                {
                    "contract": TASK_CONTEXT_CONTRACT,
                    "role": role,
                    "cycle_ref": cycle_ref,
                    "thread_id": thread_id,
                    "thread_ref": thread_ref,
                    "operation_id": operation_id,
                    "recorded_at": utc_now(),
                },
                backup=False,
            )
        return public
    finally:
        lock.__exit__(None, None, None)


def candidate_task_context(
    owner: Path,
    *,
    change_id: str,
    operation_id: str,
    definition_digest: str | None,
    review_base_sha: str | None,
    canonical_review_id: str | None,
    canonical_review_result_digest: str | None,
    remediation_manifest_digest: str | None,
    record: bool,
) -> dict[str, Any]:
    """Bind or inspect the implementation cycle represented by a candidate."""
    if canonical_review_id is not None:
        if not canonical_review_result_digest or not remediation_manifest_digest:
            return unavailable_task_context("implementation")
        components = {
            "change_id": change_id,
            "canonical_review_id": canonical_review_id,
            "canonical_review_result_digest": canonical_review_result_digest,
            "remediation_manifest_digest": remediation_manifest_digest,
        }
    else:
        if not definition_digest or not review_base_sha:
            return unavailable_task_context("implementation")
        components = {
            "change_id": change_id,
            "definition_approval_digest": definition_digest,
            "review_base_sha": review_base_sha,
        }
    return bind_task_context(
        owner,
        change_id=change_id,
        role="implementation",
        cycle_ref=task_cycle_ref(role="implementation", components=components),
        operation_id=operation_id,
        record=record,
    )


def review_task_context(
    owner: Path,
    *,
    change_id: str,
    operation_id: str,
    review_id: str | None,
    pack_digest: str | None,
    record: bool,
    allow_cross_role: bool = False,
) -> dict[str, Any]:
    """Bind or inspect the independent review cycle represented by a pack."""
    if not review_id or not pack_digest:
        return unavailable_task_context("review")
    components = {
        "change_id": change_id,
        "review_id": review_id,
        "pack_digest": pack_digest,
    }
    return bind_task_context(
        owner,
        change_id=change_id,
        role="review",
        cycle_ref=task_cycle_ref(role="review", components=components),
        operation_id=operation_id,
        record=record,
        allow_cross_role=allow_cross_role,
    )


def _owner_root(root: Path, change_id: str) -> tuple[Path, str]:
    candidate = root.resolve()
    if StateStore(candidate).path(change_id).is_file():
        return candidate, "current-checkout"
    return resolve_registered_worktree(candidate, change_id), "registered-worktree"


def _normalize_usage(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    usage = {
        key: int(value.get(key, 0))
        for key in _USAGE_KEYS
        if isinstance(value.get(key, 0), int) and int(value.get(key, 0)) >= 0
    }
    if not usage or not any(usage.values()):
        return None
    for key in _USAGE_KEYS:
        usage.setdefault(key, 0)
    usage["uncached_input_tokens"] = max(
        0, usage["input_tokens"] - usage["cached_input_tokens"]
    )
    usage["processed_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return usage


def _sum_usage(values: Iterable[dict[str, int]]) -> dict[str, int]:
    total = {key: 0 for key in _USAGE_KEYS}
    for usage in values:
        for key in _USAGE_KEYS:
            total[key] += int(usage.get(key, 0))
    total["uncached_input_tokens"] = max(
        0, total["input_tokens"] - total["cached_input_tokens"]
    )
    total["processed_tokens"] = total["input_tokens"] + total["output_tokens"]
    return total


def _reported_zero_usage(output: bytes) -> bool:
    found = False
    for raw_line in output.splitlines():
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        values = [usage.get(key) for key in _USAGE_KEYS]
        if any(isinstance(value, int) and value > 0 for value in values):
            return False
        if any(isinstance(value, int) for value in values):
            found = True
    return found


def _lane_usage_details(
    owner: Path, entry: dict[str, Any]
) -> tuple[dict[str, int] | None, str, str | None]:
    usage = _normalize_usage(entry.get("usage"))
    if usage is not None:
        return usage, "state", None
    reported = entry.get("usage")
    if isinstance(reported, dict):
        values = [reported.get(key) for key in _USAGE_KEYS]
        if any(isinstance(value, int) for value in values) and not any(
            isinstance(value, int) and value > 0 for value in values
        ):
            lane = entry.get("lane_key") or "lane"
            return None, "reported-zero", f"{lane}-reported-zero"
    relative = entry.get("transcript_path")
    if not isinstance(relative, str):
        return None, "unavailable", "missing-transcript"
    transcript = safe_resolve(owner, relative)
    if not transcript.is_file():
        return None, "unavailable", "missing-transcript"
    raw = transcript.read_bytes()
    usage = _normalize_usage(_codex_usage_from_output(raw))
    if usage is not None:
        return usage, "transcript", None
    if _reported_zero_usage(raw):
        lane = entry.get("lane_key") or "lane"
        return None, "reported-zero", f"{lane}-reported-zero"
    return None, "unavailable", "usage-unavailable"


def _lane_context_metadata(
    owner: Path, entry: dict[str, Any], *, verbose: bool
) -> dict[str, Any] | None:
    relative = entry.get("context_manifest_path")
    if not isinstance(relative, str):
        return None
    path = safe_resolve(owner, relative)
    if not path.is_file():
        return None
    manifest = read_json(path)
    totals = manifest.get("totals") if isinstance(manifest.get("totals"), dict) else {}
    inputs = manifest.get("inputs") if isinstance(manifest.get("inputs"), list) else []
    result: dict[str, Any] = {
        "mode": manifest.get("context_mode") or "unknown",
        "bytes": int(totals.get("bytes", 0) or 0),
        "words": int(totals.get("words", 0) or 0),
        "input_count": len(inputs),
    }
    if verbose:
        reasons = Counter(
            str(item.get("reason") or "unknown")
            for item in inputs
            if isinstance(item, dict)
        )
        result["input_counts_by_reason"] = dict(sorted(reasons.items()))
    return result


def record_review_task_reference(
    owner: Path,
    *,
    change_id: str,
    review_id: str,
    operation_id: str,
    task_context: dict[str, Any] | None = None,
) -> None:
    """Persist only a hashed-public local pointer to the current Codex task."""
    thread_id = os.environ.get("CODEX_THREAD_ID")
    if not thread_id:
        return
    rollout = _find_rollout(thread_id)
    turn_id: str | None = None
    turn_started_at: str | None = None
    baseline_usage: dict[str, int] | None = None
    if rollout is not None:
        turn_id, turn_started_at, baseline_usage = _current_turn_marker(rollout)
    relative = f"{TELEMETRY_ROOT}/{change_id}/{review_id}.json"
    atomic_write_json(
        safe_resolve(owner, relative),
        {
            "contract": METRICS_CONTRACT,
            "thread_id": thread_id,
            "thread_ref": hashlib.sha256(thread_id.encode("utf-8")).hexdigest(),
            "turn_id": turn_id,
            "turn_started_at": turn_started_at,
            "baseline_usage": baseline_usage,
            "operation_id": operation_id,
            "recorded_at": utc_now(),
            "task_context": task_context or unavailable_task_context("review"),
        },
        backup=False,
    )


def _find_rollout(thread_id: str) -> Path | None:
    codex_root = Path.home() / ".codex"
    matches: list[Path] = []
    for directory in (codex_root / "sessions", codex_root / "archived_sessions"):
        if directory.is_dir():
            matches.extend(directory.glob(f"**/rollout-*-{thread_id}.jsonl"))
    return max(matches, key=lambda item: item.stat().st_mtime) if matches else None


def _current_turn_marker(
    path: Path,
) -> tuple[str | None, str | None, dict[str, int] | None]:
    """Find the active turn and cumulative usage immediately before it."""
    last_usage: dict[str, int] | None = None
    turn_id: str | None = None
    turn_started_at: str | None = None
    baseline: dict[str, int] | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict) or item.get("type") != "event_msg":
                    continue
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") == "token_count":
                    info = payload.get("info")
                    total = info.get("total_token_usage") if isinstance(info, dict) else None
                    normalized = _normalize_usage(total)
                    if normalized is not None:
                        last_usage = normalized
                elif payload.get("type") == "task_started":
                    candidate = payload.get("turn_id")
                    if isinstance(candidate, str) and candidate:
                        turn_id = candidate
                        turn_started_at = item.get("timestamp")
                        baseline = dict(last_usage) if last_usage is not None else None
    except (OSError, UnicodeError):
        return None, None, None
    return turn_id, turn_started_at, baseline


def _usage_delta(
    current: dict[str, int], baseline: dict[str, int] | None
) -> dict[str, int]:
    if baseline is None:
        return current
    values = {
        key: max(0, int(current.get(key, 0)) - int(baseline.get(key, 0)))
        for key in _USAGE_KEYS
    }
    values["uncached_input_tokens"] = max(
        0, values["input_tokens"] - values["cached_input_tokens"]
    )
    values["processed_tokens"] = values["input_tokens"] + values["output_tokens"]
    return values


def _codex_task_usage(
    path: Path,
    *,
    turn_id: str | None,
    baseline_usage: dict[str, int] | None,
) -> tuple[dict[str, int] | None, bool]:
    """Read only usage and lifecycle events for one recorded Codex turn."""
    snapshot = _codex_task_snapshot(
        path,
        turn_id=turn_id,
        baseline_usage=baseline_usage,
    )
    return snapshot["usage"], snapshot["task_complete"]


def _codex_task_snapshot(
    path: Path,
    *,
    turn_id: str | None,
    baseline_usage: dict[str, int] | None,
) -> dict[str, Any]:
    """Read only event types, lifecycle markers, and token samples."""
    latest: dict[str, int] | None = None
    completed = False
    active = turn_id is None
    counts = {
        "model_messages": 0,
        "tool_calls": 0,
        "tool_outputs": 0,
        "token_samples": 0,
    }
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                item_type = item.get("type")
                payload = item.get("payload")
                if not isinstance(payload, dict):
                    continue
                event_type = payload.get("type") if item_type == "event_msg" else None
                if event_type == "task_started" and payload.get("turn_id") == turn_id:
                    active = True
                elif event_type == "task_started" and active and turn_id is not None:
                    break
                if not active:
                    continue
                if item_type == "response_item":
                    response_type = payload.get("type")
                    if response_type == "message":
                        counts["model_messages"] += 1
                    elif response_type == "function_call":
                        counts["tool_calls"] += 1
                    elif response_type == "function_call_output":
                        counts["tool_outputs"] += 1
                elif event_type == "token_count":
                    counts["token_samples"] += 1
                    info = payload.get("info")
                    total = info.get("total_token_usage") if isinstance(info, dict) else None
                    normalized = _normalize_usage(total)
                    if normalized is not None:
                        latest = _usage_delta(normalized, baseline_usage)
                elif (
                    event_type == "task_complete"
                    and active
                    and (turn_id is None or payload.get("turn_id") == turn_id)
                ):
                    completed = True
                    break
    except (OSError, UnicodeError):
        return {"usage": None, "task_complete": False, "event_counts": counts}
    return {"usage": latest, "task_complete": completed, "event_counts": counts}


def _review_selection(
    state: dict[str, Any], review_id: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    entries = [
        item
        for item in state.get("reviews", [])
        if isinstance(item, dict) and item.get("kind") in {"pack", "result"}
    ]
    if review_id is None:
        selected = next(
            (item.get("review_id") for item in reversed(entries) if item.get("review_id")),
            None,
        )
    else:
        selected = review_id
    result = next(
        (
            item
            for item in reversed(entries)
            if item.get("kind") == "result" and item.get("review_id") == selected
        ),
        None,
    )
    return selected, result


def review_metrics(
    root: Path,
    *,
    change_id: str,
    review_id: str | None = None,
    refresh: bool = False,
    verbose: bool = False,
) -> dict[str, Any]:
    owner, owner_selection = _owner_root(root, change_id)
    state = StateStore(owner).load(change_id)
    selected, result_entry = _review_selection(state, review_id)
    attempts = [
        item
        for item in state.get("reviews", [])
        if isinstance(item, dict)
        and item.get("review_id") == selected
        and isinstance(item.get("lane_key"), str)
    ]
    lanes: list[dict[str, Any]] = []
    measured: list[dict[str, int]] = []
    missing = 0
    completeness_reasons: list[str] = []
    total_command_events = 0
    for entry in attempts:
        lane_key = entry["lane_key"]
        usage, usage_source, incomplete_reason = _lane_usage_details(owner, entry)
        if usage is None:
            missing += 1
            if incomplete_reason:
                completeness_reasons.append(incomplete_reason)
        else:
            measured.append(usage)
        command_events = entry.get("command_events")
        if isinstance(command_events, int) and command_events >= 0:
            total_command_events += command_events
        cached_ratio = None
        per_command = None
        if usage is not None and usage["input_tokens"] > 0:
            cached_ratio = round(
                usage["cached_input_tokens"] / usage["input_tokens"], 6
            )
        if (
            usage is not None
            and isinstance(command_events, int)
            and command_events > 0
        ):
            per_command = round(usage["processed_tokens"] / command_events, 3)
        lane = {
            "lane": lane_key,
            "status": entry.get("status"),
            "model": entry.get("model"),
            "reasoning_effort": entry.get("reasoning_effort"),
            "attempt": entry.get("attempt_ordinal"),
            "duration_seconds": entry.get("duration_seconds"),
            "command_events": command_events,
            "usage": usage,
            "usage_source": usage_source,
            "cached_input_ratio": cached_ratio,
            "processed_tokens_per_command_event": per_command,
            "context": _lane_context_metadata(owner, entry, verbose=verbose),
        }
        lane["repair"] = entry.get("repair_contract") is not None
        lane["retry"] = int(entry.get("attempt_ordinal", 1) or 1) > 1
        if verbose:
            lane["attempt_id"] = entry.get("attempt_id")
            lane["elapsed_seconds"] = entry.get("duration_seconds")
        lanes.append(lane)
    child_usage = _sum_usage(measured) if measured else None
    if attempts and missing == 0:
        usage_status = "complete"
    elif measured:
        usage_status = "partial"
    else:
        usage_status = "unavailable"

    controller_usage: dict[str, int] | None = None
    controller_complete: bool | None = None
    controller_ref: str | None = None
    controller_counts = {
        "model_messages": 0,
        "tool_calls": 0,
        "tool_outputs": 0,
        "token_samples": 0,
    }
    controller_source = "unavailable"
    task_context = unavailable_task_context("review")
    if selected:
        relative_telemetry = Path(TELEMETRY_ROOT) / change_id / f"{selected}.json"
        unresolved = owner.resolve() / relative_telemetry
        current = owner.resolve()
        for part in relative_telemetry.parts:
            current = current / part
            if current.is_symlink():
                raise IntegrityError(
                    f"DLS review telemetry path contains a symlink: {current}"
                )
        telemetry_path = safe_resolve(owner, relative_telemetry)
        if telemetry_path.is_file():
            try:
                telemetry = read_json(telemetry_path)
            except IntegrityError:
                telemetry = {}
            controller_ref = telemetry.get("thread_ref")
            recorded_context = telemetry.get("task_context")
            if isinstance(recorded_context, dict):
                task_context = recorded_context
            thread_id = telemetry.get("thread_id")
            if isinstance(thread_id, str):
                rollout = _find_rollout(thread_id)
                if rollout is not None:
                    snapshot = _codex_task_snapshot(
                        rollout,
                        turn_id=telemetry.get("turn_id"),
                        baseline_usage=telemetry.get("baseline_usage"),
                    )
                    controller_usage = snapshot["usage"]
                    controller_complete = snapshot["task_complete"]
                    controller_counts = snapshot["event_counts"]
                    if controller_usage is not None:
                        controller_source = "transcript"
    known_parts = [item for item in (child_usage, controller_usage) if item is not None]
    all_in = _sum_usage(known_parts) if known_parts else None
    all_in_kind = (
        "exact"
        if controller_usage is not None and controller_complete and usage_status == "complete"
        else "lower-bound"
    )
    child_cached_ratio = None
    child_per_command = None
    if child_usage is not None and child_usage["input_tokens"] > 0:
        child_cached_ratio = round(
            child_usage["cached_input_tokens"] / child_usage["input_tokens"], 6
        )
    if child_usage is not None and total_command_events > 0:
        child_per_command = round(
            child_usage["processed_tokens"] / total_command_events, 3
        )
    controller_share = None
    if (
        controller_usage is not None
        and all_in is not None
        and all_in["processed_tokens"] > 0
    ):
        controller_share = round(
            controller_usage["processed_tokens"] / all_in["processed_tokens"], 6
        )
    result = {
        "ok": selected is not None,
        "contract": METRICS_CONTRACT,
        "change_id": change_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "review_id": selected,
        "review_completed": result_entry is not None,
        "usage_status": usage_status,
        "completeness_reasons": sorted(set(completeness_reasons)),
        "child_usage": child_usage,
        "child_derived": {
            "cached_input_ratio": child_cached_ratio,
            "processed_tokens_per_command_event": child_per_command,
            "command_events": total_command_events,
        },
        "controller": {
            "task_ref": controller_ref,
            "task_complete": controller_complete,
            "usage": controller_usage,
            "usage_source": controller_source,
            "event_counts": controller_counts,
        },
        "all_in": {
            "kind": all_in_kind,
            "usage": all_in,
            "controller_share_of_measured_usage": controller_share,
        },
        "task_context": task_context,
        "lanes": lanes,
    }
    if not verbose and len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 12288:
        raise IntegrityError("review-metrics payload exceeds 12 KiB; use --verbose")
    return result


def _cache_inventory(owner: Path) -> list[dict[str, Any]]:
    cache = safe_resolve(owner, ".dls/cache")
    if not cache.exists():
        return []
    output: list[dict[str, Any]] = []
    for path in cache.rglob("*"):
        if path.is_symlink():
            raise IntegrityError(f"DLS cache contains a symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(owner).as_posix()
        output.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "modified_at": datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc
                ).isoformat().replace("+00:00", "Z"),
            }
        )
    return output


def cache_status(root: Path, *, change_id: str | None = None) -> dict[str, Any]:
    owner = root.resolve()
    owner_selection = "current-checkout"
    if change_id is not None:
        owner, owner_selection = _owner_root(root, change_id)
    inventory = _cache_inventory(owner)
    if change_id is not None:
        marker = f"/{change_id}/"
        inventory = [item for item in inventory if marker in item["path"]]
    return {
        "ok": True,
        "change_id": change_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "files": len(inventory),
        "bytes": sum(item["bytes"] for item in inventory),
    }


def _protected_review_ids(state: dict[str, Any]) -> set[str]:
    reviews = [
        item
        for item in state.get("reviews", [])
        if isinstance(item, dict) and isinstance(item.get("review_id"), str)
    ]
    protected = {
        item["review_id"]
        for item in reviews
        if item.get("status") in {"running", "failed", "failed-finalize"}
        or item.get("kind") == "pack" and not any(
            other.get("kind") == "result" and other.get("review_id") == item["review_id"]
            for other in reviews
        )
    }
    completed: list[str] = []
    for item in reversed(reviews):
        if item.get("kind") == "result" and item["review_id"] not in completed:
            completed.append(item["review_id"])
    protected.update(completed[:COMPLETED_REVIEW_RETENTION])
    return protected


def _protected_cache_paths(state: dict[str, Any]) -> set[str]:
    protected: set[str] = set()
    runs = [item for item in state.get("candidate_runs", []) if isinstance(item, dict)]
    latest_failure = next(
        (
            item.get("validation_failure")
            for item in reversed(runs)
            if isinstance(item.get("validation_failure"), dict)
        ),
        None,
    )
    if isinstance(latest_failure, dict):
        for key in ("log_path", "evidence_path"):
            if isinstance(latest_failure.get(key), str):
                protected.add(latest_failure[key])
    for run in runs:
        if run.get("status") not in {"running", "blocked", "failed"}:
            continue
        for command in run.get("commands", []):
            if not isinstance(command, dict):
                continue
            for key in ("log_path", "evidence_path"):
                if isinstance(command.get(key), str):
                    protected.add(command[key])
    return protected


def cache_prune(
    root: Path, *, change_id: str | None = None, apply: bool = False
) -> dict[str, Any]:
    owner = root.resolve()
    states: list[dict[str, Any]] = []
    if change_id is not None:
        owner, _ = _owner_root(root, change_id)
        states = [StateStore(owner).load(change_id)]
    else:
        state_root = safe_resolve(owner, ".dls/state")
        if state_root.is_dir():
            states = [read_json(path) for path in state_root.glob("*.json")]
    protected = set().union(*(_protected_review_ids(state) for state in states)) if states else set()
    protected_paths = (
        set().union(*(_protected_cache_paths(state) for state in states))
        if states
        else set()
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=RAW_RETENTION_DAYS)
    inventory = _cache_inventory(owner)
    selected: list[dict[str, Any]] = []
    for item in inventory:
        path = item["path"]
        if change_id is not None and f"/{change_id}/" not in path:
            continue
        modified = datetime.fromisoformat(item["modified_at"].replace("Z", "+00:00"))
        if (
            modified >= cutoff
            or path in protected_paths
            or any(f"/{review_id}/" in path for review_id in protected)
        ):
            continue
        selected.append(item)
    if apply:
        for item in selected:
            target = safe_resolve(owner, item["path"], must_exist=True)
            if target.is_symlink() or not target.is_file():
                raise IntegrityError(f"Refusing unsafe cache removal: {target}")
            target.unlink()
        cache = safe_resolve(owner, ".dls/cache")
        for directory in sorted(
            (item for item in cache.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
    return {
        "ok": True,
        "dry_run": not apply,
        "changed": bool(selected) and apply,
        "change_id": change_id,
        "owner_root": str(owner),
        "files": len(selected),
        "bytes": sum(item["bytes"] for item in selected),
        "paths": [item["path"] for item in selected],
    }


def delivery_status(root: Path, *, change_id: str) -> dict[str, Any]:
    from .candidate_runner import candidate_status
    from .review_runner import review_status

    owner, owner_selection = _owner_root(root, change_id)
    candidate = candidate_status(owner, change_id=change_id)
    review = review_status(owner, change_id=change_id)
    cache = cache_status(owner, change_id=change_id)
    metrics = review_metrics(owner, change_id=change_id)
    review_is_current = bool(
        review.get("review_id")
        and review.get("exact_head")
        and review.get("candidate_head") == candidate.get("current_head")
    )
    if review.get("status") in {
        "running",
        "preparing-candidate",
        "failed",
        "failed-finalize",
        "ready",
    }:
        next_action = review["next_action"]
    elif review.get("review_result_path"):
        if review.get("verdict") == "review-clear":
            next_action = {
                "id": "accept-review",
                "detail": review["review_result_path"],
            }
        elif review.get("remediation_manifest_path"):
            next_action = {
                "id": "remediate-findings",
                "detail": review["remediation_manifest_path"],
            }
        else:
            next_action = {
                "id": "resolve-review-blocker",
                "detail": review["review_result_path"],
            }
    elif candidate.get("next_action", {}).get("id") == "open-review-task":
        next_action = candidate["next_action"]
    else:
        next_action = candidate["next_action"]
    result = {
        "ok": True,
        "change_id": change_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "head_sha": git_head(owner),
        "current_head": candidate.get("current_head"),
        "candidate_head": (
            review.get("candidate_head")
            if review_is_current
            else candidate.get("candidate_head")
        ),
        "exact_head": (
            review.get("exact_head", False)
            if review_is_current
            else candidate.get("exact_head", False)
        ),
        "prepared": (
            review.get("prepared", False)
            if review_is_current
            else candidate.get("prepared", False)
        ),
        "candidate": {
            "status": candidate.get("status"),
            "phase": candidate.get("phase"),
            "review_id": (
                review.get("review_id")
                if review_is_current
                else candidate.get("review_id")
            ),
        },
        "review": {
            "status": review.get("status"),
            "review_id": review.get("review_id"),
            "verdict": review.get("verdict"),
        },
        "usage_status": metrics.get("usage_status"),
        "cache_bytes": cache["bytes"],
        "next_action": next_action,
        "task_context": (
            review.get("task_context")
            if review_is_current
            else candidate.get("task_context")
        ),
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 2048:
        raise IntegrityError("delivery-status payload exceeds 2 KiB")
    return result
