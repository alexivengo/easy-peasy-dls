"""Privacy-preserving review telemetry, delivery status, and cache retention."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .errors import IntegrityError
from .io import atomic_write_json, read_json, safe_resolve, utc_now
from .operations import _codex_usage_from_output
from .repo import git_head
from .state import StateStore
from .worktrees import resolve_registered_worktree

METRICS_CONTRACT = "dls-review-metrics/v1"
TELEMETRY_ROOT = ".dls/cache/telemetry"
RAW_RETENTION_DAYS = 14
COMPLETED_REVIEW_RETENTION = 2

_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
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


def _lane_usage(owner: Path, entry: dict[str, Any]) -> dict[str, int] | None:
    usage = _normalize_usage(entry.get("usage"))
    if usage is not None:
        return usage
    relative = entry.get("transcript_path")
    if not isinstance(relative, str):
        return None
    transcript = safe_resolve(owner, relative)
    if not transcript.is_file():
        return None
    return _normalize_usage(_codex_usage_from_output(transcript.read_bytes()))


def record_review_task_reference(
    owner: Path,
    *,
    change_id: str,
    review_id: str,
    operation_id: str,
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
        },
        backup=False,
    )


def _find_rollout(thread_id: str) -> Path | None:
    sessions = Path.home() / ".codex" / "sessions"
    if not sessions.is_dir():
        return None
    matches = list(sessions.glob(f"**/rollout-*-{thread_id}.jsonl"))
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
    latest: dict[str, int] | None = None
    completed = False
    active = turn_id is None
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
                event_type = payload.get("type")
                if event_type == "task_started" and payload.get("turn_id") == turn_id:
                    active = True
                elif event_type == "task_started" and active and turn_id is not None:
                    break
                elif event_type == "token_count" and active:
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
        return None, False
    return latest, completed


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
    for entry in attempts:
        lane_key = entry["lane_key"]
        usage = _lane_usage(owner, entry)
        if usage is None:
            missing += 1
        else:
            measured.append(usage)
        lane = {
            "lane": lane_key,
            "status": entry.get("status"),
            "model": entry.get("model"),
            "reasoning_effort": entry.get("reasoning_effort"),
            "attempt": entry.get("attempt_ordinal"),
            "duration_seconds": entry.get("duration_seconds"),
            "command_events": entry.get("command_events"),
            "usage": usage,
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
    if selected:
        telemetry_path = safe_resolve(
            owner, f"{TELEMETRY_ROOT}/{change_id}/{selected}.json"
        )
        if telemetry_path.is_file():
            telemetry = read_json(telemetry_path)
            controller_ref = telemetry.get("thread_ref")
            thread_id = telemetry.get("thread_id")
            if isinstance(thread_id, str):
                rollout = _find_rollout(thread_id)
                if rollout is not None:
                    controller_usage, controller_complete = _codex_task_usage(
                        rollout,
                        turn_id=telemetry.get("turn_id"),
                        baseline_usage=telemetry.get("baseline_usage"),
                    )
    known_parts = [item for item in (child_usage, controller_usage) if item is not None]
    all_in = _sum_usage(known_parts) if known_parts else None
    all_in_kind = (
        "exact"
        if controller_usage is not None and controller_complete and usage_status == "complete"
        else "lower-bound"
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
        "child_usage": child_usage,
        "controller": {
            "task_ref": controller_ref,
            "task_complete": controller_complete,
            "usage": controller_usage,
        },
        "all_in": {"kind": all_in_kind, "usage": all_in},
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
    if review.get("status") == "running":
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
        "candidate": {
            "status": candidate.get("status"),
            "phase": candidate.get("phase"),
            "review_id": candidate.get("review_id"),
        },
        "review": {
            "status": review.get("status"),
            "review_id": review.get("review_id"),
            "verdict": review.get("verdict"),
        },
        "usage_status": metrics.get("usage_status"),
        "cache_bytes": cache["bytes"],
        "next_action": next_action,
    }
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 2048:
        raise IntegrityError("delivery-status payload exceeds 2 KiB")
    return result
