"""End-to-end, state-owned DLS review orchestration."""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .decisions import decision_readiness, review_pack_decisions_current
from .delivery_receipt import delivery_receipt
from .errors import IntegrityError, LockError
from .economy import (
    ReviewBudget,
    processed_tokens,
    review_budget,
    token_budget_failure,
    token_budget_warning,
)
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
    COMMAND_EVENT_CONTRACT,
    NATIVE_REVIEW_MAX_OUTPUT_BYTES,
    NATIVE_REVIEW_TIMEOUT_SECONDS,
    NATIVE_REVIEW_TRANSCRIPT_MAX_BYTES,
    RETRYABLE_REVIEW_LANE_STATUSES,
    REVIEW_LANE_MAX_ATTEMPTS,
    REVIEW_IDENTIFIER_CONTRACT,
    REVIEW_DECISION_REPAIR_CONTRACT,
    REVIEW_RUNNER_CONTRACT,
    _all_review_findings,
    _attempt_lease_expired,
    _codex_usage_from_output,
    _existing_remediation_manifest_path,
    _finding_blocks,
    _latest_review_result,
    _native_plaintext_projection,
    _process_is_alive,
    _read_review_result,
    _review_actionable_findings,
    _resolve_review_pack,
    _review_lane_entries,
    _semantic_review_effort,
    _validate_review_pack_current,
    _validate_review_pack,
    _validate_review_report,
    _run_bounded_command,
    review_import,
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
from .state import derived_approval_statuses
from .review_presentation import build_review_presentation
from .state import StateStore
from .telemetry import (
    record_review_task_reference,
    review_task_context,
    unavailable_task_context,
)
from .worktrees import registry_routes_change_elsewhere, resolve_change_root

SEMANTIC_MODEL = "gpt-5.6-sol"
SPECIALIST_MODEL = "gpt-5.6-terra"
SPECIALIST_EFFORT = "high"
REVIEW_PROMPTS_ROOT = PLUGIN_ROOT / "assets" / "review-prompts"
DECISION_REPAIR_INPUT_MAX_BYTES = 262144
INPUT_ONLY_REVIEW_MAX_BYTES = 2 * 1024 * 1024
FINAL_FULL_COMMAND_TARGET = 16
FINAL_FULL_COMMAND_CEILING = 24
FINAL_FULL_COMMAND_BUDGET_CONTRACT = "dls-final-command-budget/v1"
FINAL_FULL_TIMEOUT_SECONDS = 900
FINAL_FULL_TRANSCRIPT_BYTES = 1024 * 1024
REVIEW_BUDGET_CONTRACT = "dls-review-budget/v2"
FINAL_COVERAGE_CONTRACT = "dls-final-coverage/v1"
BOUND_CONTEXT_INPUT_CONTRACT = "dls-bound-context-inputs/v1"
BOUND_CONTEXT_INPUT_PATHS = {
    "active-review-pack": "bound/review-pack.json",
    "filtered-requirements-projection": "bound/requirements.json",
}


def _installed_final_full_command_ceiling(
    owner: Path,
    control_level: str,
) -> int:
    return min(
        review_budget(owner, control_level).command_events,
        FINAL_FULL_COMMAND_CEILING,
    )


def _final_full_budget(owner: Path, control_level: str) -> ReviewBudget:
    budget = review_budget(owner, control_level)
    return ReviewBudget(
        aggregate_tokens=budget.aggregate_tokens,
        lane_tokens=budget.lane_tokens,
        command_events=_installed_final_full_command_ceiling(owner, control_level),
        timeout_seconds=min(budget.timeout_seconds, FINAL_FULL_TIMEOUT_SECONDS),
        transcript_bytes=min(budget.transcript_bytes, FINAL_FULL_TRANSCRIPT_BYTES),
        aggregate_recovery_tokens=budget.aggregate_ceiling,
        lane_recovery_tokens=budget.lane_ceiling,
    )


def _budget_projection(budget: ReviewBudget) -> dict[str, int]:
    return {
        "aggregate_tokens": budget.aggregate_tokens,
        "aggregate_recovery_tokens": budget.aggregate_ceiling,
        "lane_tokens": budget.lane_tokens,
        "lane_recovery_tokens": budget.lane_ceiling,
        "command_events": budget.command_events,
        "timeout_seconds": budget.timeout_seconds,
        "transcript_bytes": budget.transcript_bytes,
    }


class ReviewDecisionReferenceError(IntegrityError):
    """A model decision contains unsafe or inconsistent identifier links."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid-reference",
        path: str = "$",
        prior_finding_id: str | None = None,
        invalid_value: Any = None,
        repairable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.prior_finding_id = prior_finding_id
        self.invalid_value = invalid_value
        self.repairable = repairable

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "message": str(self),
            "prior_finding_id": self.prior_finding_id,
            "invalid_value": self.invalid_value,
            "repairable": self.repairable,
        }


class ReviewBudgetPlanningError(IntegrityError):
    """A required review lane cannot be launched inside its bounded input plan."""


def _ticket_alias_index(pack: dict[str, Any]) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = {}
    change_prefix = f"{pack['change_id']}-"
    for canonical in pack["tickets"]:
        candidates = {canonical}
        if canonical.startswith(change_prefix):
            suffix = canonical[len(change_prefix) :]
            candidates.add(suffix)
            match = re.fullmatch(r"T([0-9]+)", suffix)
            if match:
                candidates.add(f"T-{match.group(1)}")
        for candidate in candidates:
            aliases.setdefault(candidate, set()).add(canonical)
    return aliases


def _normalize_structured_payload(
    payload: dict[str, Any],
    *,
    pack: dict[str, Any],
    payload_kind: str,
    lens_id: str | None,
    reference_errors: list[ReviewDecisionReferenceError] | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Validate model-owned references and return a canonical DLS projection.

    Raw model output remains immutable. Only exact ticket IDs and conservative,
    unique aliases derived from the active ReviewPack are accepted.
    """

    _validate_structured_payload(
        payload,
        payload_kind=payload_kind,
        lens_id=lens_id,
    )
    normalized = copy.deepcopy(payload)
    findings = normalized.get("findings", [])
    if not isinstance(findings, list):
        raise ReviewDecisionReferenceError("Review findings must be an array")
    aliases = _ticket_alias_index(pack)
    normalizations: list[dict[str, str]] = []
    finding_ids: set[str] = set()
    for finding_index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise ReviewDecisionReferenceError(
                "Each review finding must be an object",
                code="invalid-finding",
                path=f"$.findings[{finding_index}]",
                invalid_value=finding,
                repairable=False,
            )
        finding_id = finding.get("id")
        if (
            not isinstance(finding_id, str)
            or not finding_id
            or finding_id in finding_ids
        ):
            raise ReviewDecisionReferenceError(
                f"Invalid or duplicate review finding ID: {finding_id!r}",
                code="invalid-finding-id",
                path=f"$.findings[{finding_index}].id",
                invalid_value=finding_id,
                repairable=False,
            )
        finding_ids.add(finding_id)
        for field in ("ticket_ids", "requirement_ids"):
            values = finding.get(field)
            if (
                not isinstance(values, list)
                or not all(isinstance(item, str) and item for item in values)
                or len(values) != len(set(values))
            ):
                raise ReviewDecisionReferenceError(
                    f"Finding {finding_id} has invalid {field}",
                    code="invalid-reference-list",
                    path=f"$.findings[{finding_index}].{field}",
                    invalid_value=values,
                    repairable=False,
                )
        canonical_values: list[str] = []
        for source in finding["ticket_ids"]:
            targets = aliases.get(source, set())
            if len(targets) != 1:
                reason = "unknown" if not targets else "ambiguous"
                error = ReviewDecisionReferenceError(
                    f"Finding {finding_id} references {reason} ticket: {source}",
                    code=f"{reason}-ticket-id",
                    path=f"$.findings[{finding_index}].ticket_ids",
                    invalid_value=source,
                )
                if reference_errors is None:
                    raise error
                reference_errors.append(error)
                continue
            canonical = next(iter(targets))
            if canonical in canonical_values:
                error = ReviewDecisionReferenceError(
                    f"Finding {finding_id} has duplicate ticket after normalization: "
                    f"{canonical}",
                    code="duplicate-ticket-id",
                    path=f"$.findings[{finding_index}].ticket_ids",
                    invalid_value=canonical,
                )
                if reference_errors is None:
                    raise error
                reference_errors.append(error)
                continue
            canonical_values.append(canonical)
            if source != canonical:
                normalizations.append(
                    {
                        "finding_id": finding_id,
                        "field": "ticket_ids",
                        "source": source,
                        "canonical": canonical,
                        "rule": "unique-ticket-alias",
                    }
                )
        finding["ticket_ids"] = canonical_values

    if payload_kind == "specialist":
        return normalized, normalizations

    required_prior = {
        item["finding_id"]: item
        for item in pack.get("required_prior_findings", [])
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    }
    actual_prior: dict[str, dict[str, Any]] = {}
    for prior_index, item in enumerate(normalized["prior_finding_verdicts"]):
        if not isinstance(item, dict):
            raise ReviewDecisionReferenceError(
                "Prior finding verdict must be an object",
                code="invalid-prior-verdict",
                path=f"$.prior_finding_verdicts[{prior_index}]",
                invalid_value=item,
                repairable=False,
            )
        finding_id = item.get("finding_id")
        if (
            not isinstance(finding_id, str)
            or finding_id not in required_prior
            or finding_id in actual_prior
        ):
            raise ReviewDecisionReferenceError(
                f"Invalid or duplicate prior finding verdict: {finding_id!r}",
                code="invalid-prior-finding-id",
                path=f"$.prior_finding_verdicts[{prior_index}].finding_id",
                prior_finding_id=finding_id if isinstance(finding_id, str) else None,
                invalid_value=finding_id,
                repairable=False,
            )
        verdict = item.get("verdict")
        replacement = item.get("replacement_finding_id")
        if verdict in {"still-open", "regressed"}:
            if replacement == finding_id:
                error = ReviewDecisionReferenceError(
                    f"Prior finding {finding_id} cannot replace itself",
                    code="replacement-reuses-prior-id",
                    path=(
                        f"$.prior_finding_verdicts[{prior_index}]"
                        ".replacement_finding_id"
                    ),
                    prior_finding_id=finding_id,
                    invalid_value=replacement,
                )
                if reference_errors is None:
                    raise error
                reference_errors.append(error)
            elif not isinstance(replacement, str):
                error = ReviewDecisionReferenceError(
                    f"Prior finding {finding_id} requires a replacement finding",
                    code="missing-replacement-finding",
                    path=(
                        f"$.prior_finding_verdicts[{prior_index}]"
                        ".replacement_finding_id"
                    ),
                    prior_finding_id=finding_id,
                    invalid_value=replacement,
                )
                if reference_errors is None:
                    raise error
                reference_errors.append(error)
            elif replacement not in finding_ids:
                error = ReviewDecisionReferenceError(
                    f"Prior finding {finding_id} references an unknown replacement: "
                    f"{replacement}",
                    code="unknown-replacement-finding",
                    path=(
                        f"$.prior_finding_verdicts[{prior_index}]"
                        ".replacement_finding_id"
                    ),
                    prior_finding_id=finding_id,
                    invalid_value=replacement,
                )
                if reference_errors is None:
                    raise error
                reference_errors.append(error)
        elif replacement is not None:
            error = ReviewDecisionReferenceError(
                f"Prior finding {finding_id} cannot declare a replacement",
                code="unexpected-replacement-finding",
                path=(
                    f"$.prior_finding_verdicts[{prior_index}]"
                    ".replacement_finding_id"
                ),
                prior_finding_id=finding_id,
                invalid_value=replacement,
            )
            if reference_errors is None:
                raise error
            reference_errors.append(error)
        if verdict == "waived":
            disposition = required_prior[finding_id].get("disposition")
            if not isinstance(disposition, dict) or disposition.get("status") != "waived":
                raise ReviewDecisionReferenceError(
                    f"Prior finding {finding_id} has no current human waiver",
                    code="missing-human-waiver",
                    path=f"$.prior_finding_verdicts[{prior_index}].verdict",
                    prior_finding_id=finding_id,
                    invalid_value=verdict,
                    repairable=False,
                )
        actual_prior[finding_id] = item
    missing_prior = sorted(set(required_prior) - set(actual_prior))
    if missing_prior:
        raise ReviewDecisionReferenceError(
            "Review decision is missing prior finding verdicts: "
            + ", ".join(missing_prior),
            code="missing-prior-verdicts",
            path="$.prior_finding_verdicts",
            invalid_value=missing_prior,
            repairable=False,
        )
    return normalized, normalizations


def _collect_decision_reference_errors(
    payload: dict[str, Any],
    *,
    pack: dict[str, Any],
) -> list[ReviewDecisionReferenceError]:
    """Return every safely classifiable reference error in one pass.

    Normal review validation still fails on its first invalid reference. Repair
    uses this collector so one compact call sees every independent cross-field
    error instead of discovering them through repeated model calls. Structural
    or unsafe errors still raise immediately and are never auto-repaired.
    """

    errors: list[ReviewDecisionReferenceError] = []
    _normalize_structured_payload(
        payload,
        pack=pack,
        payload_kind="decision",
        lens_id=None,
        reference_errors=errors,
    )
    return errors


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
    input_bundle_digest: str | None = None,
    budget: ReviewBudget,
) -> str:
    contract: dict[str, Any] = {
        "runner_contract": pack.get("runner_contract", REVIEW_RUNNER_CONTRACT),
        "review_id": pack["review_id"],
        "lane_key": lane_key,
        "pack_digest": pack["pack_digest"],
        "head_sha": pack["head_sha"],
        "model": model,
        "reasoning_effort": effort,
        "prompt_digest": prompt_digest,
        "schema_digest": schema_digest,
        "context_digest": context_digest,
        "input_bundle_digest": input_bundle_digest,
    }
    if pack.get("budget_contract") == REVIEW_BUDGET_CONTRACT:
        contract["budget_contract"] = REVIEW_BUDGET_CONTRACT
        contract["budget"] = _budget_projection(budget)
    else:
        contract["budget"] = {
            "aggregate_tokens": budget.aggregate_tokens,
            "lane_tokens": budget.lane_tokens,
            "command_events": budget.command_events,
            "timeout_seconds": budget.timeout_seconds,
            "transcript_bytes": budget.transcript_bytes,
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
    owner = resolve_change_root(candidate, change_id)
    selection = "current-checkout" if owner == candidate else "registered-worktree"
    return owner, selection


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
        if pack.get("control_level") == "routine" and pack.get("runner_contract") == REVIEW_RUNNER_CONTRACT:
            projected = 1
        else:
            projected = int("native-diff" in pack.get("required_lanes", []))
            if pack.get("review_mode") == "full":
                projected += len(pack.get("risk_lenses", []))
            projected += 1  # independent semantic
            projected += 1  # conditional compact reconciliation or final-full
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
    *,
    owner: Path | None = None,
    control_level: str | None = None,
) -> dict[str, str]:
    lane_key = terminal.get("lane_key")
    if terminal.get("status") == "budget-exceeded":
        reason = terminal.get("failure_reason")
        recorded_budget = terminal.get("budget")
        recorded_command_limit = (
            recorded_budget.get("command_events")
            if isinstance(recorded_budget, dict)
            else None
        )
        command_events = terminal.get("command_events")
        failure_kind = terminal.get("budget_failure_kind")
        if (
            lane_key == "semantic:final-full"
            and owner is not None
            and control_level in {"standard", "critical"}
            and terminal.get("exit_code") == 126
            and terminal.get("timed_out") is False
            and terminal.get("overflow") is False
            and terminal.get("transcript_truncated") is not True
            and failure_kind in {None, "command-events"}
            and isinstance(recorded_command_limit, int)
            and isinstance(command_events, int)
            and recorded_command_limit < _installed_final_full_command_ceiling(
                owner, control_level
            )
            and command_events <= _installed_final_full_command_ceiling(
                owner, control_level
            )
            and len(
                [
                    item
                    for item in lane_attempts
                    if item.get("lane_key") == lane_key
                ]
            )
            < REVIEW_LANE_MAX_ATTEMPTS
        ):
            return {
                "id": "resume-review-command-budget",
                "detail": (
                    "legacy final-full command budget is recoverable: "
                    f"used={command_events}, limit={recorded_command_limit}; "
                    "resume reuses completed lanes and reruns only final-full "
                    "with hard ceiling="
                    f"{_installed_final_full_command_ceiling(owner, control_level)}"
                ),
            }
        if (
            owner is not None
            and control_level in {"routine", "standard", "critical"}
            and isinstance(reason, str)
            and reason.startswith(
                ("lane processed_tokens=", "aggregate processed_tokens=")
            )
            and terminal.get("exit_code") == 0
            and terminal.get("timed_out") is False
            and terminal.get("overflow") is False
            and terminal.get("transcript_truncated") is not True
        ):
            usage_tokens = processed_tokens(terminal.get("usage"))
            aggregate_total = sum(
                value
                for item in lane_attempts
                for value in [processed_tokens(item.get("usage"))]
                if value is not None
            )
            budget = review_budget(owner, control_level)
            if usage_tokens is not None and token_budget_failure(
                terminal.get("usage"),
                aggregate_before=max(0, aggregate_total - usage_tokens),
                budget=budget,
            ) is None:
                return {
                    "id": "resume-review-budget",
                    "detail": (
                        "the completed structured output is eligible for "
                        "zero-call bounded recovery"
                    ),
                }
        return {
            "id": "inspect-review-budget",
            "detail": terminal.get("failure_reason")
            or "the review exceeded its risk-adjusted execution budget",
        }
    if terminal.get("status") == "invalid-output":
        validation_error = terminal.get("validation_error")
        if terminal.get("lane_key") == "native":
            recovery_status = terminal.get("native_recovery_status")
            if recovery_status == "integrity-failed":
                return {
                    "id": "inspect-review-integrity",
                    "detail": terminal.get("native_recovery_error")
                    or "the native output or transcript failed integrity checks",
                }
            if recovery_status == "unsafe":
                return {
                    "id": "inspect-review-output",
                    "detail": terminal.get("native_recovery_error")
                    or "the native plaintext cannot be safely recovered",
                }
            return {
                "id": "resume-review",
                "detail": terminal.get("failure_reason")
                or "the native output may have a deterministic installed projection",
            }
        if (
            isinstance(lane_key, str)
            and lane_key.endswith(":repair")
        ) or (
            isinstance(validation_error, dict)
            and validation_error.get("repairable") is False
        ):
            return {
                "id": "inspect-review-output",
                "detail": terminal.get("failure_reason")
                or "the bounded repair output is still invalid",
            }
        return {
            "id": "resume-review-repair",
            "detail": terminal.get("failure_reason")
            or "a compact decision-reference repair is available",
        }
    if terminal.get("status") == "source-changed":
        return {
            "id": "inspect-review-integrity",
            "detail": "the source snapshot changed during a review lane",
        }
    current_schema_digest: str | None = None
    if isinstance(lane_key, str) and lane_key.startswith("specialist:"):
        current_schema_digest = sha256_file(
            SCHEMAS_ROOT / "specialist-decision.schema.json"
        )
    elif lane_key in {
        "semantic:full",
        "semantic:targeted",
        "semantic:final-full",
        "routine:terra",
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
        and len(matching) < int(terminal.get("max_attempts", REVIEW_LANE_MAX_ATTEMPTS))
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
    _inspect_task_context: bool = True,
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
    prior_review_id: str | None = None
    prior_review_result_path: str | None = None
    prior_reviewed_head: str | None = None
    prior_remediation_manifest_path: str | None = None
    active_lane = any(
        item.get("status") == "running" for item in latest_by_lane.values()
    )
    from .candidate_runner import candidate_status

    candidate: dict[str, Any] | None = candidate_status(
        owner,
        change_id=change_id,
        _inspect_task_context=_inspect_task_context,
    )
    decision_gate = decision_readiness(
        owner,
        state,
        derived_approval_statuses(owner, state),
        require_definition=state.get("control_level") in {"standard", "critical"},
    )
    if result_entry:
        result_path, result_report = _read_review_result(owner, result_entry)
        remediation_manifest_path = _existing_remediation_manifest_path(
            owner,
            change_id=change_id,
            review_entry=result_entry,
            review_id=result_entry["review_id"],
        )
        needs_remediation = bool(_review_actionable_findings(result_report))
        status_value = "completed"
        if remediation_manifest_path is not None:
            next_action = {
                "id": "remediate-findings",
                "detail": remediation_manifest_path,
            }
        elif needs_remediation:
            next_action = {
                "id": "recover-remediation-manifest",
                "detail": (
                    f"latest review {result_entry['review_id']} has no canonical "
                    "remediation manifest"
                ),
            }
        elif result_report.get("verdict") == "review-clear":
            next_action = {"id": "accept-review", "detail": result_path}
        else:
            next_action = {"id": "resolve-review-blocker", "detail": result_path}
    elif active_lane or pipeline_alive:
        status_value = "running"
        next_action = {"id": "wait-review", "detail": "review pipeline is active"}
    elif terminal_lane is not None:
        status_value = "failed"
        terminal_pack = None
        if pack_entry and isinstance(pack_entry.get("pack_path"), str):
            terminal_pack = read_json(
                safe_resolve(owner, pack_entry["pack_path"], must_exist=True)
            )
        next_action = _terminal_lane_next_action(
            terminal_lane,
            lane_attempts,
            owner=owner,
            control_level=(
                terminal_pack.get("control_level")
                if isinstance(terminal_pack, dict)
                else state.get("control_level")
            ),
        )
    elif pipeline is not None and pipeline.get("status") in {
        "failed",
        "failed-finalize",
    }:
        status_value = pipeline["status"]
        failure_kind = pipeline.get("failure_kind")
        if failure_kind == "model-output":
            action_id = "resume-review-repair"
        elif failure_kind == "invalid-repair-output":
            action_id = "inspect-review-output"
        elif failure_kind == "budget-exceeded":
            action_id = "inspect-review-budget"
        elif failure_kind == "review-context":
            action_id = "split-review-scope"
        elif failure_kind == "integrity":
            action_id = "inspect-review-integrity"
        else:
            action_id = (
                "resume-review" if status_value == "failed-finalize" else "retry-review"
            )
        next_action = {
            "id": action_id,
            "detail": pipeline.get("failure_reason") or pipeline.get("stage") or "review failed",
        }
    elif pipeline is not None and pipeline.get("status") == "running":
        status_value = "failed"
        next_action = {
            "id": (
                "resume-review-repair"
                if pipeline.get("failure_kind") == "model-output"
                else "resume-review"
            ),
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
        latest_result = None
        if candidate is not None and candidate.get("status") == "running":
            status_value = "preparing-candidate"
            next_action = {
                "id": "wait-review",
                "detail": "trusted candidate preparation is active",
            }
        else:
            latest_result = _latest_review_result(state)
        if candidate is not None and candidate.get("status") == "running":
            pass
        elif latest_result is None or review_id is not None:
            status_value = "not-prepared"
            next_action = {
                "id": "prepare-candidate",
                "detail": (
                    "return to the implementation task and complete candidate-ready "
                    "for the current HEAD; the first review also requires --base BASE"
                ),
            }
        else:
            prior_review_id = latest_result.get("review_id")
            prior_review_result_path = latest_result.get("result_path")
            if isinstance(prior_review_id, str):
                remediation_manifest_path = _existing_remediation_manifest_path(
                    owner,
                    change_id=change_id,
                    review_entry=latest_result,
                    review_id=prior_review_id,
                )
            if (
                latest_result.get("verdict") in {"not-clear", "blocked"}
                and remediation_manifest_path is None
            ):
                status_value = "blocked"
                next_action = {
                    "id": "recover-remediation-manifest",
                    "detail": (
                        f"latest review {prior_review_id} has no canonical "
                        "remediation manifest"
                    ),
                }
            else:
                status_value = "not-prepared"
                next_action = {
                    "id": "prepare-candidate",
                    "detail": (
                        "return to the implementation/remediation task and complete "
                        f"candidate-ready for current HEAD {current_head}"
                    ),
                }
    if (
        not decision_gate["ready"]
        and result_entry is None
        and active_lane is False
        and pipeline is None
        and terminal_lane is None
    ):
        status_value = "not-prepared"
        next_action = decision_gate["next_action"]
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
        _validate_review_pack(pack, change_id)
        if provenance_pack_entry.get("pack_digest") != pack.get("pack_digest"):
            raise IntegrityError("ReviewPack digest does not match DLS state")
        pack_decisions_current = review_pack_decisions_current(
            pack.get("decisions"), decision_gate["decisions"]
        )
        if (
            result_entry is None
            and pack_entry is provenance_pack_entry
            and decision_gate["ready"]
            and pack_decisions_current
        ):
            _validate_review_pack_current(owner, state=state, pack=pack)
        elif result_entry is None and pack_entry is provenance_pack_entry and (
            not decision_gate["ready"] or not pack_decisions_current
        ):
            status_value = "not-prepared"
            next_action = (
                decision_gate["next_action"]
                if not decision_gate["ready"]
                else {
                    "id": "run-candidate-ready",
                    "detail": "design or architecture decision changed after ReviewPack creation",
                }
            )
        runner_contract = pack.get("runner_contract", runner_contract)
        prior = pack.get("prior_review")
        if isinstance(prior, dict):
            prior_review_id = prior.get("review_id")
            prior_review_result_path = prior.get("result_path")
            prior_reviewed_head = prior.get("head_sha")
            prior_remediation_manifest_path = prior.get(
                "remediation_manifest_path"
            )
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
    if _inspect_task_context:
        task_context = review_task_context(
            owner,
            change_id=change_id,
            operation_id=str(
                (pipeline or {}).get("operation_id")
                or selected_review_id
                or "review-status"
            ),
            review_id=pack.get("review_id") if isinstance(pack, dict) else None,
            pack_digest=pack.get("pack_digest") if isinstance(pack, dict) else None,
            record=False,
            allow_cross_role=bool(
                isinstance(pack, dict) and pack.get("control_level") == "routine"
            ),
        )
    else:
        task_context = unavailable_task_context("review")
    pack_exact = bool(
        pack_entry
        and isinstance(pack, dict)
        and pack.get("head_sha") == current_head
        and candidate is not None
        and candidate.get("prepared")
        and candidate.get("review_id") == selected_review_id
        and decision_gate["ready"]
        and review_pack_decisions_current(
            pack.get("decisions"), decision_gate["decisions"]
        )
    )
    result_exact = bool(
        result_entry
        and result_entry.get("head_sha") == current_head
    )
    payload = {
        "ok": True,
        "changed": False,
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "current_head": current_head,
        "candidate_head": pack.get("head_sha") if isinstance(pack, dict) else None,
        "exact_head": result_exact if result_entry is not None else pack_exact,
        "prepared": result_exact if result_entry is not None else pack_exact,
        "review_id": selected_review_id,
        "prior_review_id": prior_review_id,
        "prior_review_result_path": prior_review_result_path,
        "prior_reviewed_head": prior_reviewed_head,
        "prior_remediation_manifest_path": prior_remediation_manifest_path,
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
        "decisions": decision_gate["decisions"],
        "next_action": next_action,
        "task_context": task_context,
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


def _review_prompt_values(pack: dict[str, Any]) -> dict[str, str]:
    return {
        "CHANGE_ID": pack["change_id"],
        "REVIEW_ID": pack["review_id"],
        "COMPARISON_BASE_SHA": pack["comparison_base_sha"],
        "EPIC_BASE_SHA": pack["epic_base_sha"],
        "HEAD_SHA": pack["head_sha"],
        "CANONICAL_TICKET_IDS": json.dumps(
            list(pack["tickets"]),
            ensure_ascii=False,
        ),
        "REQUIRED_PRIOR_FINDING_IDS": json.dumps(
            [
                item["finding_id"]
                for item in pack.get("required_prior_findings", [])
            ],
            ensure_ascii=False,
        ),
    }


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
    input_only: bool = False,
) -> tuple[Path, Path]:
    temporary_parent = Path(tempfile.mkdtemp(prefix="dls-review-"))
    workspace = temporary_parent / "checkout"
    try:
        context_source = safe_resolve(owner, context_path, must_exist=True)
        if input_only:
            workspace.mkdir(parents=True)
            run_git(workspace, "init")
        else:
            run_git(
                owner,
                "worktree",
                "add",
                "--detach",
                str(workspace),
                pack["head_sha"],
            )
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
        if not input_only:
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
        if input_only and (input_root / "context.json").is_file():
            _validate_bound_context_workspace(input_root)
        return workspace, temporary_parent
    except Exception:
        if not input_only:
            run_git(owner, "worktree", "remove", "--force", str(workspace), check=False)
        shutil.rmtree(temporary_parent, ignore_errors=True)
        raise


def _cleanup_isolated_workspace(
    owner: Path,
    workspace: Path,
    temporary_parent: Path,
    *,
    input_only: bool = False,
) -> None:
    if not input_only:
        run_git(owner, "worktree", "remove", "--force", str(workspace), check=False)
    shutil.rmtree(temporary_parent, ignore_errors=True)
    if not input_only:
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


def _input_bundle_metadata(
    extra_files: dict[str, Path | bytes],
) -> tuple[str | None, int]:
    if not extra_files:
        return None, 0
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for relative, value in sorted(extra_files.items()):
        content = value if isinstance(value, bytes) else value.read_bytes()
        total_bytes += len(content)
        entries.append(
            {
                "path": relative,
                "bytes": len(content),
                "sha256": sha256_bytes(content),
            }
        )
    return (
        sha256_bytes(
            json.dumps(entries, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ),
        total_bytes,
    )


def _bound_context_inputs(
    owner: Path,
    context_path: str,
) -> tuple[dict[str, Path | bytes], dict[str, Any]]:
    """Bind compact context dependencies inside the input-only workspace."""
    context_source = safe_resolve(owner, context_path, must_exist=True)
    context = read_json(context_source)
    context_digest = context.get("manifest_digest")
    if not isinstance(context_digest, str) or not context_digest:
        raise IntegrityError("Review context is missing its manifest digest")

    selected: dict[str, dict[str, Any]] = {}
    files: dict[str, Path | bytes] = {}
    for item in context.get("inputs", []):
        if not isinstance(item, dict):
            continue
        reason = item.get("reason")
        if reason not in BOUND_CONTEXT_INPUT_PATHS:
            continue
        if reason in selected:
            raise IntegrityError(f"Review context contains duplicate {reason} input")
        relative = item.get("path")
        expected_digest = item.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected_digest, str):
            raise IntegrityError(f"Review context {reason} input is incomplete")
        source = safe_resolve(owner, relative, must_exist=True)
        actual_digest = sha256_file(source)
        if actual_digest != expected_digest:
            raise IntegrityError(f"Review context {reason} input digest mismatch")
        stable_path = BOUND_CONTEXT_INPUT_PATHS[reason]
        size = source.stat().st_size
        selected[reason] = {
            "reason": reason,
            "path": stable_path,
            "sha256": actual_digest,
            "bytes": size,
        }
        files[stable_path] = source

    if "active-review-pack" not in selected:
        raise IntegrityError("Review context is missing its active ReviewPack projection")

    manifest = {
        "contract": BOUND_CONTEXT_INPUT_CONTRACT,
        "review_context_digest": context_digest,
        "inputs": [
            selected[reason]
            for reason in BOUND_CONTEXT_INPUT_PATHS
            if reason in selected
        ],
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    manifest_digest = sha256_bytes(manifest_bytes)
    files["bound-inputs.json"] = manifest_bytes
    return files, {
        "bound_context_contract": BOUND_CONTEXT_INPUT_CONTRACT,
        "bound_context_digest": manifest_digest,
        "bound_context_input_count": len(selected),
        "bound_context_bytes": sum(item["bytes"] for item in selected.values()),
    }


def _validate_bound_context_workspace(input_root: Path) -> None:
    context_path = input_root / "context.json"
    manifest_path = input_root / "bound-inputs.json"
    if not context_path.is_file() or not manifest_path.is_file():
        raise IntegrityError("Input-only review is missing its bound context bundle")
    context = read_json(context_path)
    manifest = read_json(manifest_path)
    if manifest.get("contract") != BOUND_CONTEXT_INPUT_CONTRACT:
        raise IntegrityError("Bound context input contract is invalid")
    if manifest.get("review_context_digest") != context.get("manifest_digest"):
        raise IntegrityError("Bound context does not match the review context digest")

    expected: dict[str, str] = {}
    for item in context.get("inputs", []):
        if isinstance(item, dict) and item.get("reason") in BOUND_CONTEXT_INPUT_PATHS:
            reason = item["reason"]
            if reason in expected:
                raise IntegrityError(f"Review context contains duplicate {reason} input")
            digest = item.get("sha256")
            if not isinstance(digest, str):
                raise IntegrityError(f"Review context {reason} input is incomplete")
            expected[reason] = digest
    if "active-review-pack" not in expected:
        raise IntegrityError("Review context is missing its active ReviewPack projection")

    entries = manifest.get("inputs")
    if not isinstance(entries, list):
        raise IntegrityError("Bound context input manifest is incomplete")
    actual: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise IntegrityError("Bound context input entry is invalid")
        reason = entry.get("reason")
        if reason not in BOUND_CONTEXT_INPUT_PATHS or reason in actual:
            raise IntegrityError("Bound context input reason is invalid or duplicated")
        stable_path = BOUND_CONTEXT_INPUT_PATHS[reason]
        if entry.get("path") != stable_path:
            raise IntegrityError("Bound context input path is not canonical")
        destination = safe_resolve(input_root, stable_path, must_exist=True)
        digest = sha256_file(destination)
        if digest != entry.get("sha256") or digest != expected.get(reason):
            raise IntegrityError(f"Bound context {reason} digest mismatch")
        if destination.stat().st_size != entry.get("bytes"):
            raise IntegrityError(f"Bound context {reason} byte count mismatch")
        actual[reason] = digest
    if actual != expected:
        raise IntegrityError("Bound context input set is incomplete")


def _final_full_inputs(
    owner: Path,
    *,
    pack: dict[str, Any],
    context_path: str,
    native_bytes: bytes,
    independent_decision: dict[str, Any],
    targeted_decision: dict[str, Any],
    specialist_payloads: dict[str, bytes],
    aggregate_before: int,
    budget: ReviewBudget,
    prompt_text: str = "",
    schema_path: Path | None = None,
) -> tuple[dict[str, Path | bytes], dict[str, Any]]:
    """Build an exact, input-only whole-change bundle for final remediation review."""
    actual_paths = sorted(
        line
        for line in run_git(
            owner,
            "diff",
            "--name-only",
            f"{pack['epic_base_sha']}..{pack['head_sha']}",
        ).stdout.splitlines()
        if line
    )
    expected_paths = sorted(pack.get("full_changed_files", []))
    if actual_paths != expected_paths:
        raise IntegrityError("Final coverage paths differ from the bound ReviewPack")
    patch_bytes = run_git(
        owner,
        "diff",
        "--no-ext-diff",
        "--binary",
        f"{pack['epic_base_sha']}..{pack['head_sha']}",
    ).stdout.encode("utf-8")
    coverage_entries: list[dict[str, Any]] = []
    for relative in actual_paths:
        blob = run_git(
            owner,
            "rev-parse",
            "--verify",
            f"{pack['head_sha']}:{relative}",
            check=False,
        )
        coverage_entries.append(
            {
                "path": relative,
                "head_blob": blob.stdout.strip() if blob.returncode == 0 else None,
                "deleted": blob.returncode != 0,
            }
        )
    coverage = {
        "contract": FINAL_COVERAGE_CONTRACT,
        "review_id": pack["review_id"],
        "epic_base_sha": pack["epic_base_sha"],
        "head_sha": pack["head_sha"],
        "path_count": len(coverage_entries),
        "paths": coverage_entries,
        "patch_digest": sha256_bytes(patch_bytes),
        "patch_bytes": len(patch_bytes),
    }
    coverage_bytes = json.dumps(
        coverage,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    budget_plan = {
        "contract": REVIEW_BUDGET_CONTRACT,
        "command_budget_contract": FINAL_FULL_COMMAND_BUDGET_CONTRACT,
        "command_target": min(
            FINAL_FULL_COMMAND_TARGET,
            budget.command_events,
        ),
        "command_ceiling": budget.command_events,
        "review_id": pack["review_id"],
        "lane_key": "semantic:final-full",
        "aggregate_before": aggregate_before,
        "aggregate_target_remaining": max(
            0, budget.aggregate_tokens - aggregate_before
        ),
        "aggregate_recovery_remaining": max(
            0, budget.aggregate_ceiling - aggregate_before
        ),
        "budget": _budget_projection(budget),
        "coverage_digest": sha256_bytes(coverage_bytes),
    }
    bound_files, bound_metadata = _bound_context_inputs(owner, context_path)
    extra_files: dict[str, Path | bytes] = {
        "context.json": safe_resolve(owner, context_path, must_exist=True),
        "epic.patch": patch_bytes,
        "coverage.json": coverage_bytes,
        "budget-plan.json": json.dumps(
            budget_plan,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8"),
        "native.txt": native_bytes,
        "semantic-independent.json": json.dumps(
            independent_decision,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8"),
        "targeted-decision.json": json.dumps(
            targeted_decision,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8"),
        **{
            f"specialists/{name}": payload
            for name, payload in specialist_payloads.items()
        },
        **bound_files,
    }
    input_digest, extra_input_bytes = _input_bundle_metadata(extra_files)
    effective_schema_path = schema_path or (SCHEMAS_ROOT / "review-decision.schema.json")
    prompt_bytes = prompt_text.encode("utf-8")
    schema_bytes = effective_schema_path.read_bytes()
    fixed_input_bytes = len(prompt_bytes) + len(schema_bytes)
    input_bytes = extra_input_bytes + fixed_input_bytes
    full_input_digest = sha256_bytes(
        json.dumps(
            {
                "extra_digest": input_digest,
                "prompt_digest": sha256_bytes(prompt_bytes),
                "pack_digest": pack["pack_digest"],
                "schema_digest": sha256_bytes(schema_bytes),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if input_bytes > INPUT_ONLY_REVIEW_MAX_BYTES:
        raise ReviewBudgetPlanningError(
            "Final-full input bundle exceeds the 2 MiB bounded coverage limit"
        )
    metadata = {
        "budget_contract": REVIEW_BUDGET_CONTRACT,
        "command_budget_contract": FINAL_FULL_COMMAND_BUDGET_CONTRACT,
        "command_target": min(
            FINAL_FULL_COMMAND_TARGET,
            budget.command_events,
        ),
        "command_ceiling": budget.command_events,
        "final_coverage_contract": FINAL_COVERAGE_CONTRACT,
        "final_coverage_digest": sha256_bytes(coverage_bytes),
        "final_coverage_path_count": len(coverage_entries),
        "final_patch_digest": sha256_bytes(patch_bytes),
        "final_patch_bytes": len(patch_bytes),
        "final_input_bundle_digest": full_input_digest,
        "final_input_bundle_bytes": input_bytes,
        "budget_plan": budget_plan,
        "workspace_mode": "input-only",
        **bound_metadata,
    }
    return extra_files, metadata


def _completed_lane_payload(
    owner: Path,
    entry: dict[str, Any],
    *,
    pack: dict[str, Any],
    payload_kind: str,
    lens_id: str | None,
) -> dict[str, Any]:
    output_path = entry.get("output_path")
    if not isinstance(output_path, str):
        raise IntegrityError("Completed review lane is missing output_path")
    path = safe_resolve(owner, output_path, must_exist=True)
    if sha256_file(path) != entry.get("output_digest"):
        raise IntegrityError("Completed review lane output digest mismatch")
    normalized_path_value = entry.get("normalized_output_path")
    normalized_digest = entry.get("normalized_output_digest")
    if isinstance(normalized_path_value, str) or isinstance(normalized_digest, str):
        if not isinstance(normalized_path_value, str) or not isinstance(
            normalized_digest, str
        ):
            raise IntegrityError("Completed review lane normalized projection is incomplete")
        normalized_path = safe_resolve(owner, normalized_path_value, must_exist=True)
        if sha256_file(normalized_path) != normalized_digest:
            raise IntegrityError(
                "Completed review lane normalized projection digest mismatch"
            )
        payload = read_json(normalized_path)
    else:
        payload = read_json(path)
    normalized, _ = _normalize_structured_payload(
        payload,
        pack=pack,
        payload_kind=payload_kind,
        lens_id=lens_id,
    )
    if isinstance(normalized_path_value, str) and payload != normalized:
        raise IntegrityError(
            "Completed review lane normalized projection content mismatch"
        )
    return normalized


def _recover_completed_token_budget_lane(
    owner: Path,
    *,
    state_store: StateStore,
    change_id: str,
    pack: dict[str, Any],
    attempts: list[dict[str, Any]],
    lane_contract_digest: str,
    effective_budget: ReviewBudget,
    payload_kind: str,
    lens_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Accept an already completed decision under a newer bounded budget.

    Command, duration, transcript, transport and integrity failures are never
    eligible. The raw output and transcript remain immutable provenance.
    """

    current_snapshot = git_source_snapshot_digest(owner)
    state = state_store.load(change_id)
    aggregate_total = sum(
        value
        for item in state.get("reviews", [])
        if isinstance(item, dict) and item.get("review_id") == pack["review_id"]
        for value in [processed_tokens(item.get("usage"))]
        if value is not None
    )
    for candidate in reversed(attempts):
        if candidate.get("status") == "completed" and candidate.get(
            "budget_recovery_lane_contract_digest"
        ) == lane_contract_digest:
            return candidate, _completed_lane_payload(
                owner,
                candidate,
                pack=pack,
                payload_kind=payload_kind,
                lens_id=lens_id,
            )
        reason = candidate.get("failure_reason")
        if (
            candidate.get("status") != "budget-exceeded"
            or not isinstance(reason, str)
            or not reason.startswith(
                ("lane processed_tokens=", "aggregate processed_tokens=")
            )
            or candidate.get("exit_code") != 0
            or candidate.get("timed_out") is not False
            or candidate.get("overflow") is not False
            or candidate.get("transcript_truncated") is True
            or candidate.get("head_sha") != pack["head_sha"]
            or candidate.get("pack_digest") != pack["pack_digest"]
            or candidate.get("source_snapshot_before") != current_snapshot
            or candidate.get("source_snapshot_digest") != current_snapshot
        ):
            continue
        usage_tokens = processed_tokens(candidate.get("usage"))
        if usage_tokens is None:
            continue
        if token_budget_failure(
            candidate.get("usage"),
            aggregate_before=max(0, aggregate_total - usage_tokens),
            budget=effective_budget,
        ) is not None:
            continue
        recorded_budget = candidate.get("budget")
        command_limit = effective_budget.command_events
        timeout_limit = effective_budget.timeout_seconds
        transcript_limit = effective_budget.transcript_bytes
        if isinstance(recorded_budget, dict):
            command_limit = int(
                recorded_budget.get("command_events", command_limit)
            )
            timeout_limit = int(
                recorded_budget.get("timeout_seconds", timeout_limit)
            )
            transcript_limit = int(
                recorded_budget.get("transcript_bytes", transcript_limit)
            )
        if (
            candidate.get("command_events", 0) > command_limit
            or candidate.get("duration_seconds", 0) > timeout_limit
            or candidate.get("transcript_retained_bytes", 0) > transcript_limit
        ):
            continue
        output_relative = candidate.get("output_path")
        transcript_relative = candidate.get("transcript_path")
        attempt_id = candidate.get("attempt_id")
        if not all(
            isinstance(value, str) and value
            for value in (output_relative, transcript_relative, attempt_id)
        ):
            continue
        output_path = safe_resolve(owner, output_relative, must_exist=True)
        transcript_path = safe_resolve(owner, transcript_relative, must_exist=True)
        if (
            sha256_file(output_path) != candidate.get("output_digest")
            or sha256_file(transcript_path) != candidate.get("transcript_digest")
        ):
            raise IntegrityError("Token-budget recovery artifact digest mismatch")
        raw_payload = read_json(output_path)
        normalized, identifier_normalizations = _normalize_structured_payload(
            raw_payload,
            pack=pack,
            payload_kind=payload_kind,
            lens_id=lens_id,
        )
        safe_lane = str(candidate.get("lane_key", "lane")).replace(":", "-")
        normalized_relative = (
            f".dls/cache/reviews/{change_id}/{pack['review_id']}/"
            f"{safe_lane}-{attempt_id}.normalized.json"
        )
        normalized_path = safe_resolve(owner, normalized_relative)
        atomic_write_json(normalized_path, normalized, backup=False)
        aggregate_before = max(0, aggregate_total - usage_tokens)
        warning = token_budget_warning(
            candidate.get("usage"),
            aggregate_before=aggregate_before,
            budget=effective_budget,
        )
        try:
            _, recovered, _ = state_store.finish_review_lane(
                change_id,
                attempt_id=attempt_id,
                expected_status="budget-exceeded",
                updates={
                    "status": "completed",
                    "normalized_output_path": normalized_relative,
                    "normalized_output_digest": sha256_file(normalized_path),
                    "identifier_normalizations": identifier_normalizations,
                    "original_budget_failure_reason": reason,
                    "budget_contract": REVIEW_BUDGET_CONTRACT,
                    "budget_status": (
                        "recovered-over-target" if warning is not None else "within-target"
                    ),
                    "budget_warning": warning,
                    "budget_recovery_contract": "dls-token-budget-recovery/v2",
                    "budget_recovery_lane_contract_digest": lane_contract_digest,
                    "recovered_budget": _budget_projection(effective_budget),
                    "recovered_without_model_call": True,
                    "budget_recovered_at": utc_now(),
                    "failure_reason": None,
                },
            )
        except Exception:
            normalized_path.unlink(missing_ok=True)
            raise
        return recovered, normalized
    return None


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
    max_attempts: int = REVIEW_LANE_MAX_ATTEMPTS,
    return_invalid_output: bool = False,
    input_only_workspace: bool = False,
    attempt_metadata: dict[str, Any] | None = None,
    budget: ReviewBudget | None = None,
    stream_callback: Callable[[dict[str, Any]], None] | None = None,
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
    resolved_extra_files = extra_files or {}
    input_bundle_digest, input_bundle_bytes = _input_bundle_metadata(
        resolved_extra_files
    )
    if (
        input_only_workspace
        and lane_kind == "decision-repair"
        and input_bundle_bytes > DECISION_REPAIR_INPUT_MAX_BYTES
    ):
        raise IntegrityError("Decision repair input exceeds the 256 KiB contract limit")
    if input_only_workspace and input_bundle_bytes > INPUT_ONLY_REVIEW_MAX_BYTES:
        raise IntegrityError(
            "Input-only review bundle exceeds the 2 MiB large-context limit"
        )
    effective_budget = budget or review_budget(owner, pack["control_level"])
    lane_contract_digest = _lane_contract_digest(
        pack=pack,
        lane_key=lane_key,
        model=model,
        effort=effort,
        prompt_digest=prompt_digest,
        schema_digest=schema_digest,
        context_digest=context_digest,
        input_bundle_digest=input_bundle_digest,
        budget=effective_budget,
    )
    while True:
        state = state_store.load(change_id)
        aggregate_before = sum(
            value
            for item in state.get("reviews", [])
            if isinstance(item, dict) and item.get("review_id") == pack["review_id"]
            for value in [processed_tokens(item.get("usage"))]
            if value is not None
        )
        if aggregate_before >= effective_budget.aggregate_ceiling:
            _update_pipeline(
                owner,
                change_id=change_id,
                review_id=pack["review_id"],
                operation_id=root_operation_id,
                stage=lane_key,
                status="failed",
                failure_reason=(
                    f"aggregate processed_tokens={aggregate_before} exceeds "
                    f"ceiling={effective_budget.aggregate_ceiling}"
                ),
                failure_kind="budget-exceeded",
            )
            return {
                "status": "budget-exceeded",
                "lane_key": lane_key,
                "failure_reason": "aggregate child token recovery ceiling exhausted",
            }, None
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
        recovered_budget = _recover_completed_token_budget_lane(
            owner,
            state_store=state_store,
            change_id=change_id,
            pack=pack,
            attempts=attempts,
            lane_contract_digest=lane_contract_digest,
            effective_budget=effective_budget,
            payload_kind=payload_kind,
            lens_id=lens_id,
        )
        if recovered_budget is not None:
            return recovered_budget
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
                pack=pack,
                payload_kind=payload_kind,
                lens_id=lens_id,
            )
        terminal = contract_attempts[-1] if contract_attempts else None
        if (
            terminal
            and terminal.get("status") == "invalid-output"
            and return_invalid_output
        ):
            return terminal, None
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
        if len(contract_attempts) >= max_attempts:
            suffix = (
                f"; last_error={terminal.get('failure_reason')}"
                if terminal and terminal.get("failure_reason")
                else ""
            )
            reason = f"Review lane {lane_key} exhausted automatic attempts{suffix}"
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
        normalized_output_relative = (
            f"{cache_root}/{safe_lane}-{attempt_id}.normalized.json"
        )
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
            "max_attempts": max_attempts,
            "operation_id": operation_id,
            "runner_pid": os.getpid(),
            "runner_contract": pack.get("runner_contract", REVIEW_RUNNER_CONTRACT),
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
            "input_bundle_digest": input_bundle_digest,
            "input_bundle_bytes": input_bundle_bytes,
            "output_path": output_relative,
            "transcript_path": transcript_relative,
            "source_snapshot_before": snapshot_before,
            "started_at": utc_now(),
        }
        if attempt_metadata:
            proposed.update(copy.deepcopy(attempt_metadata))
        for lock_attempt in range(21):
            try:
                state, claimed_attempt, claimed = state_store.claim_review_lane(
                    change_id,
                    attempt=proposed,
                    operation_kind=f"review-run:{lane_key}",
                    max_attempts=max_attempts,
                )
                break
            except LockError:
                if lock_attempt == 20:
                    raise
                time.sleep(0.05)
        if not claimed:
            if claimed_attempt.get("status") == "running":
                return claimed_attempt, None
            continue
        output_path = safe_resolve(owner, output_relative)
        normalized_output_path = safe_resolve(owner, normalized_output_relative)
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
                extra_files=resolved_extra_files,
                input_only=input_only_workspace,
            )
            execution = _run_bounded_command(
                normalized_argv,
                cwd=workspace,
                environment=allowed_environment(["HOME", "CODEX_HOME"]),
                timeout_seconds=effective_budget.timeout_seconds,
                max_output_bytes=effective_budget.transcript_bytes,
                terminate_on_overflow=True,
                max_command_events=effective_budget.command_events,
                heartbeat_callback=(
                    (
                        lambda elapsed, output_bytes, command_events: stream_callback(
                            {
                                "event": "heartbeat",
                                "change_id": change_id,
                                "review_id": pack["review_id"],
                                "lane": lane_key,
                                "elapsed_seconds": round(elapsed, 1),
                                "output_bytes": output_bytes,
                                "command_events": command_events,
                            }
                        )
                    )
                    if stream_callback is not None
                    else None
                ),
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
            validation_error: dict[str, Any] | None = None
            if execution["timed_out"]:
                status_value = "budget-exceeded"
                failure_reason = (
                    "review lane exceeded duration budget="
                    f"{effective_budget.timeout_seconds}s"
                )
            elif execution["exit_code"] != 0:
                if execution.get("budget_exceeded") or execution.get("overflow"):
                    status_value = "budget-exceeded"
                    if execution.get("budget_failure_kind") == "command-events":
                        failure_reason = (
                            "review lane command budget exceeded: "
                            f"used={execution.get('command_events', 0)}, "
                            f"limit={effective_budget.command_events}"
                        )
                    else:
                        failure_reason = (
                            "review lane transcript budget exceeded: "
                            f"bytes={execution.get('output_bytes', 0)}, "
                            f"limit={effective_budget.transcript_bytes}"
                        )
                else:
                    status_value = "api-failure"
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
                    raw_payload = read_json(output_path)
                    payload, identifier_normalizations = _normalize_structured_payload(
                        raw_payload,
                        pack=pack,
                        payload_kind=payload_kind,
                        lens_id=lens_id,
                    )
                    atomic_write_json(
                        normalized_output_path,
                        payload,
                        backup=False,
                    )
                except IntegrityError as exc:
                    status_value = "invalid-output"
                    failure_reason = str(exc)
                    validation_error = (
                        exc.as_dict()
                        if isinstance(exc, ReviewDecisionReferenceError)
                        else {
                            "code": "invalid-structured-output",
                            "path": "$",
                            "message": str(exc),
                            "prior_finding_id": None,
                            "invalid_value": None,
                            "repairable": False,
                        }
                    )
            snapshot_after = git_source_snapshot_digest(owner)
            if status_value == "completed" and snapshot_after != snapshot_before:
                status_value = "source-changed"
            usage = _codex_usage_from_output(execution["output"])
            budget_failure = token_budget_failure(
                usage,
                aggregate_before=aggregate_before,
                budget=effective_budget,
            )
            budget_warning = token_budget_warning(
                usage,
                aggregate_before=aggregate_before,
                budget=effective_budget,
            )
            if status_value == "completed" and budget_failure is not None:
                status_value = "budget-exceeded"
                failure_reason = budget_failure
            final_updates = {
                "status": status_value,
                "output_path": output_relative if output_path.is_file() else None,
                "output_digest": (
                    sha256_file(output_path) if output_path.is_file() else None
                ),
                "normalized_output_path": (
                    normalized_output_relative
                    if status_value == "completed" and normalized_output_path.is_file()
                    else None
                ),
                "normalized_output_digest": (
                    sha256_file(normalized_output_path)
                    if status_value == "completed" and normalized_output_path.is_file()
                    else None
                ),
                "identifier_normalizations": (
                    identifier_normalizations if status_value == "completed" else []
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
                "usage": usage,
                "command_events": execution.get("command_events", 0),
                "command_event_contract": execution.get(
                    "command_event_contract",
                    COMMAND_EVENT_CONTRACT,
                ),
                "budget_failure_kind": execution.get("budget_failure_kind"),
                "budget_contract": REVIEW_BUDGET_CONTRACT,
                "budget": _budget_projection(effective_budget),
                "budget_status": (
                    "completed-over-target"
                    if status_value == "completed" and budget_warning is not None
                    else (
                        "within-target" if status_value == "completed" else None
                    )
                ),
                "budget_warning": (
                    budget_warning if status_value == "completed" else None
                ),
                "duration_seconds": execution["duration_seconds"],
                "source_snapshot_digest": snapshot_after,
                "failure_reason": failure_reason,
                "validation_error": validation_error,
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
                    input_only=input_only_workspace,
                )
        assert recorded is not None
        if recorded["status"] == "completed":
            assert payload is not None
            return recorded, payload
        if recorded["status"] == "invalid-output" and return_invalid_output:
            return recorded, None
        if recorded["status"] == "budget-exceeded":
            _update_pipeline(
                owner,
                change_id=change_id,
                review_id=pack["review_id"],
                operation_id=root_operation_id,
                stage=lane_key,
                status="failed",
                failure_reason=recorded.get("failure_reason"),
                failure_kind="budget-exceeded",
            )
            return recorded, None
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


def _historical_invalid_attempt(
    owner: Path,
    *,
    state: dict[str, Any],
    pack: dict[str, Any],
    lane_key: str,
) -> tuple[dict[str, Any], dict[str, Any], ReviewDecisionReferenceError] | None:
    attempts = _review_lane_entries(
        state,
        review_id=pack["review_id"],
        lane_key=lane_key,
    )
    if any(item.get("status") == "completed" for item in attempts):
        return None
    attempt = next(
        (
            item
            for item in reversed(attempts)
            if item.get("status") == "invalid-output"
        ),
        None,
    )
    if attempt is None:
        return None
    if (
        attempt.get("pack_digest") != pack["pack_digest"]
        or attempt.get("head_sha") != pack["head_sha"]
        or attempt.get("source_snapshot_digest")
        != git_source_snapshot_digest(owner)
    ):
        raise IntegrityError(
            "Historical invalid decision no longer matches the exact review source"
        )
    output_relative = attempt.get("output_path")
    output_digest = attempt.get("output_digest")
    if not isinstance(output_relative, str) or not isinstance(output_digest, str):
        raise IntegrityError("Historical invalid decision is missing immutable output")
    output_path = safe_resolve(owner, output_relative, must_exist=True)
    if sha256_file(output_path) != output_digest:
        raise IntegrityError("Historical invalid decision output digest mismatch")
    raw = read_json(output_path)
    try:
        _normalize_structured_payload(
            raw,
            pack=pack,
            payload_kind="decision",
            lens_id=None,
        )
    except ReviewDecisionReferenceError as exc:
        return attempt, raw, exc
    except IntegrityError as exc:
        raise IntegrityError(
            f"Historical invalid decision is not safely repairable: {exc}"
        ) from exc
    raise IntegrityError(
        "Historical invalid decision now validates but has no completed projection"
    )


def _canonical_prior_finding_map(
    owner: Path,
    *,
    pack: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    required_ids = {
        item["finding_id"]
        for item in pack.get("required_prior_findings", [])
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    }
    if not required_ids:
        return {}
    prior = pack.get("prior_review")
    if not isinstance(prior, dict):
        raise IntegrityError("Repair requires the digest-bound prior ReviewIR")
    relative = prior.get("result_path")
    digest = prior.get("result_digest")
    if not isinstance(relative, str) or not isinstance(digest, str):
        raise IntegrityError("Repair prior ReviewIR reference is incomplete")
    path = safe_resolve(owner, relative, must_exist=True)
    report = read_json(path)
    canonical_digest = sha256_bytes(
        json.dumps(
            report,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    if canonical_digest != digest:
        raise IntegrityError("Repair prior ReviewIR digest mismatch")
    findings = {
        item["id"]: item
        for item in report.get("findings", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    missing = sorted(required_ids - set(findings))
    if missing:
        raise IntegrityError(
            "Repair prior ReviewIR is missing findings: " + ", ".join(missing)
        )
    return {finding_id: findings[finding_id] for finding_id in sorted(required_ids)}


def _reserve_replacement_ids(
    owner: Path,
    *,
    state: dict[str, Any],
    pack: dict[str, Any],
    raw_decision: dict[str, Any],
) -> dict[str, str]:
    used = set(_all_review_findings(owner, state))
    used.update(
        item["id"]
        for item in raw_decision.get("findings", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    reservations: dict[str, str] = {}
    for item in raw_decision.get("prior_finding_verdicts", []):
        if not isinstance(item, dict) or item.get("verdict") not in {
            "still-open",
            "regressed",
        }:
            continue
        prior_id = item.get("finding_id")
        replacement = item.get("replacement_finding_id")
        if not isinstance(prior_id, str) or (
            isinstance(replacement, str)
            and replacement != prior_id
            and replacement in {
                finding.get("id")
                for finding in raw_decision.get("findings", [])
                if isinstance(finding, dict)
            }
        ):
            continue
        match = re.fullmatch(r"(.*-R)([0-9]+)", prior_id)
        if match:
            prefix = match.group(1)
            width = len(match.group(2))
        else:
            prefix = f"{pack['change_id'].replace('-', '')}-R"
            width = 3
        numbers = [
            int(candidate.group(1))
            for value in used
            if (candidate := re.fullmatch(re.escape(prefix) + r"([0-9]+)", value))
        ]
        number = max(numbers, default=0) + 1
        candidate_id = f"{prefix}{number:0{width}d}"
        while candidate_id in used:
            number += 1
            candidate_id = f"{prefix}{number:0{width}d}"
        used.add(candidate_id)
        reservations[prior_id] = candidate_id
    return reservations


def _repair_bundle(
    owner: Path,
    *,
    state: dict[str, Any],
    pack: dict[str, Any],
    original_entry: dict[str, Any],
    raw_decision: dict[str, Any],
    error: ReviewDecisionReferenceError,
) -> tuple[
    bytes,
    dict[str, str],
    str,
    list[ReviewDecisionReferenceError],
]:
    if not error.repairable:
        raise IntegrityError(
            f"Decision reference error is not safely repairable: {error}"
        )
    prior_findings = _canonical_prior_finding_map(owner, pack=pack)
    reservations = _reserve_replacement_ids(
        owner,
        state=state,
        pack=pack,
        raw_decision=raw_decision,
    )
    errors = _collect_decision_reference_errors(raw_decision, pack=pack)
    if not errors:
        # A terminal projection can surface an exact reference error after the
        # cached decision itself has normalized successfully. Preserve that
        # state-owned error as the bounded repair contract in this recovery
        # path; normal semantic failures still produce the complete list above.
        errors = [error]
    if any(not item.repairable for item in errors):
        unsafe = next(item for item in errors if not item.repairable)
        raise IntegrityError(
            f"Decision reference error is not safely repairable: {unsafe}"
        )
    bundle = {
        "contract": REVIEW_DECISION_REPAIR_CONTRACT,
        "review_id": pack["review_id"],
        "original_attempt_id": original_entry["attempt_id"],
        "original_output_digest": original_entry["output_digest"],
        "validation_errors": [item.as_dict() for item in errors],
        "allowed_ticket_ids": list(pack["tickets"]),
        "required_prior_finding_ids": list(prior_findings),
        "canonical_prior_findings": prior_findings,
        "reserved_replacement_ids": reservations,
        "raw_decision": raw_decision,
    }
    encoded = json.dumps(
        bundle,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > DECISION_REPAIR_INPUT_MAX_BYTES:
        raise IntegrityError("Decision repair bundle exceeds the 256 KiB limit")
    error_digest = sha256_bytes(
        json.dumps(
            [item.as_dict() for item in errors],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return encoded, reservations, error_digest, errors


def _validate_repaired_decision(
    *,
    original: dict[str, Any],
    repaired: dict[str, Any],
    errors: list[ReviewDecisionReferenceError],
    reservations: dict[str, str],
    prior_findings: dict[str, dict[str, Any]],
) -> None:
    for field in ("verdict", "summary"):
        if repaired.get(field) != original.get(field):
            raise ReviewDecisionReferenceError(
                f"Decision repair changed semantic field: {field}",
                code="repair-changed-semantic-decision",
                path=f"$.{field}",
                invalid_value=repaired.get(field),
                repairable=False,
            )
    original_prior = {
        item.get("finding_id"): item
        for item in original.get("prior_finding_verdicts", [])
        if isinstance(item, dict)
    }
    repaired_prior = {
        item.get("finding_id"): item
        for item in repaired.get("prior_finding_verdicts", [])
        if isinstance(item, dict)
    }
    if set(original_prior) != set(repaired_prior):
        raise ReviewDecisionReferenceError(
            "Decision repair changed the prior-finding verdict set",
            code="repair-changed-prior-set",
            path="$.prior_finding_verdicts",
            invalid_value=sorted(str(item) for item in repaired_prior),
            repairable=False,
        )
    for finding_id, source in original_prior.items():
        candidate = repaired_prior[finding_id]
        for field in ("finding_id", "verdict", "evidence"):
            if candidate.get(field) != source.get(field):
                raise ReviewDecisionReferenceError(
                    f"Decision repair changed prior verdict field: {finding_id}.{field}",
                    code="repair-changed-prior-verdict",
                    path=f"$.prior_finding_verdicts[{finding_id}].{field}",
                    prior_finding_id=(
                        finding_id if isinstance(finding_id, str) else None
                    ),
                    invalid_value=candidate.get(field),
                    repairable=False,
                )
        expected_replacement = reservations.get(str(finding_id))
        if expected_replacement is not None and candidate.get(
            "replacement_finding_id"
        ) != expected_replacement:
            raise ReviewDecisionReferenceError(
                f"Decision repair did not use reserved replacement for {finding_id}",
                code="repair-invalid-reserved-id",
                path=(
                    f"$.prior_finding_verdicts[{finding_id}]"
                    ".replacement_finding_id"
                ),
                prior_finding_id=str(finding_id),
                invalid_value=candidate.get("replacement_finding_id"),
                repairable=False,
            )
        expected_existing_link = source.get("replacement_finding_id")
        unexpected_replacements = {
            item.prior_finding_id
            for item in errors
            if item.code == "unexpected-replacement-finding"
        }
        if finding_id in unexpected_replacements:
            expected_existing_link = None
        if expected_replacement is None and candidate.get(
            "replacement_finding_id"
        ) != expected_existing_link:
            raise ReviewDecisionReferenceError(
                f"Decision repair changed a valid replacement link for {finding_id}",
                code="repair-changed-valid-link",
                path=(
                    f"$.prior_finding_verdicts[{finding_id}]"
                    ".replacement_finding_id"
                ),
                prior_finding_id=str(finding_id),
                invalid_value=candidate.get("replacement_finding_id"),
                repairable=False,
            )
    original_findings = {
        item.get("id"): item
        for item in original.get("findings", [])
        if isinstance(item, dict)
    }
    repaired_findings = {
        item.get("id"): item
        for item in repaired.get("findings", [])
        if isinstance(item, dict)
    }
    reference_repair_fields = (
        {"ticket_ids"}
        if any(
            item.code
            in {
                "unknown-ticket-id",
                "ambiguous-ticket-id",
                "duplicate-ticket-id",
            }
            for item in errors
        )
        else set()
    )
    for finding_id, finding in original_findings.items():
        candidate = repaired_findings.get(finding_id)
        if not isinstance(candidate, dict) or any(
            candidate.get(field) != value
            for field, value in finding.items()
            if field not in reference_repair_fields
        ):
            raise ReviewDecisionReferenceError(
                f"Decision repair changed existing finding: {finding_id}",
                code="repair-changed-existing-finding",
                path="$.findings",
                invalid_value=finding_id,
                repairable=False,
            )
    allowed_new = set(reservations.values())
    actual_new = set(repaired_findings) - set(original_findings)
    if actual_new != allowed_new:
        raise ReviewDecisionReferenceError(
            "Decision repair did not create exactly the reserved findings",
            code="repair-invalid-finding-set",
            path="$.findings",
            invalid_value=sorted(str(item) for item in actual_new),
            repairable=False,
        )
    for prior_id, replacement_id in reservations.items():
        prior = prior_findings[prior_id]
        replacement = repaired_findings[replacement_id]
        for field in (
            "severity",
            "kind",
            "ticket_ids",
            "requirement_ids",
            "blocks",
        ):
            if replacement.get(field) != prior.get(field):
                raise ReviewDecisionReferenceError(
                    f"Replacement {replacement_id} changed classification field: {field}",
                    code="repair-changed-finding-classification",
                    path=f"$.findings[{replacement_id}].{field}",
                    prior_finding_id=prior_id,
                    invalid_value=replacement.get(field),
                    repairable=False,
                )


def _execute_decision_repair(
    owner: Path,
    *,
    change_id: str,
    state: dict[str, Any],
    pack: dict[str, Any],
    context_path: str,
    root_operation_id: str,
    source_lane_key: str,
    original_entry: dict[str, Any],
    raw_decision: dict[str, Any],
    error: ReviewDecisionReferenceError,
    effort: str,
    stream_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    repair_bytes, reservations, error_digest, errors = _repair_bundle(
        owner,
        state=state,
        pack=pack,
        original_entry=original_entry,
        raw_decision=raw_decision,
        error=error,
    )
    prior_findings = _canonical_prior_finding_map(owner, pack=pack)
    lane_key = f"{source_lane_key}:repair"
    _update_pipeline(
        owner,
        change_id=change_id,
        review_id=pack["review_id"],
        operation_id=root_operation_id,
        stage="repairing",
        create=True,
        failure_reason=str(error),
        failure_kind="model-output",
    )
    entry, repaired = _execute_structured_lane(
        owner,
        change_id=change_id,
        pack=pack,
        context_path=context_path,
        root_operation_id=root_operation_id,
        lane_key=lane_key,
        lane_kind="decision-repair",
        model=SEMANTIC_MODEL,
        effort=effort,
        prompt_text=_render_prompt("repair-decision.md", {}),
        schema_path=SCHEMAS_ROOT / "review-decision.schema.json",
        payload_kind="decision",
        extra_files={"repair.json": repair_bytes},
        max_attempts=2,
        return_invalid_output=True,
        input_only_workspace=True,
        attempt_metadata={
            "repair_contract": REVIEW_DECISION_REPAIR_CONTRACT,
            "repair_source_lane_key": source_lane_key,
            "repair_original_attempt_id": original_entry["attempt_id"],
            "repair_original_output_digest": original_entry["output_digest"],
            "repair_error_code": (
                error.code if len(errors) == 1 else "multiple-reference-errors"
            ),
            "repair_error_digest": error_digest,
            "repair_reserved_ids": reservations,
        },
        stream_callback=stream_callback,
    )
    if repaired is None:
        if entry.get("status") == "running":
            return entry, None
        reason = entry.get("failure_reason") or "decision repair failed"
        _update_pipeline(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
            operation_id=root_operation_id,
            stage="repairing",
            status="failed",
            failure_reason=reason,
            failure_kind="invalid-repair-output",
        )
        raise IntegrityError(
            f"Decision repair failed without another model retry: {reason}"
        )
    _validate_repaired_decision(
        original=raw_decision,
        repaired=repaired,
        errors=errors,
        reservations=reservations,
        prior_findings=prior_findings,
    )
    return entry, repaired


def _execute_decision_lane(
    owner: Path,
    *,
    change_id: str,
    pack: dict[str, Any],
    context_path: str,
    root_operation_id: str,
    lane_key: str,
    lane_kind: str,
    effort: str,
    prompt_text: str,
    extra_files: dict[str, Path | bytes] | None = None,
    input_only_workspace: bool = False,
    attempt_metadata: dict[str, Any] | None = None,
    budget: ReviewBudget | None = None,
    model: str = SEMANTIC_MODEL,
    stream_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    state = StateStore(owner).load(change_id)
    historical = _historical_invalid_attempt(
        owner,
        state=state,
        pack=pack,
        lane_key=lane_key,
    )
    if historical is None:
        entry, decision = _execute_structured_lane(
            owner,
            change_id=change_id,
            pack=pack,
            context_path=context_path,
            root_operation_id=root_operation_id,
            lane_key=lane_key,
            lane_kind=lane_kind,
            model=model,
            effort=effort,
            prompt_text=prompt_text,
            schema_path=SCHEMAS_ROOT / "review-decision.schema.json",
            payload_kind="decision",
            extra_files=extra_files,
            return_invalid_output=True,
            input_only_workspace=input_only_workspace,
            attempt_metadata=attempt_metadata,
            budget=budget,
            stream_callback=stream_callback,
        )
        if decision is not None or entry.get("status") == "running":
            return entry, decision
        if entry.get("status") != "invalid-output":
            return entry, None
        if not isinstance(entry.get("output_path"), str):
            raise IntegrityError(
                "Invalid semantic output is missing its immutable output artifact"
            )
        output_path = safe_resolve(owner, entry["output_path"], must_exist=True)
        raw_decision = read_json(output_path)
        try:
            _normalize_structured_payload(
                raw_decision,
                pack=pack,
                payload_kind="decision",
                lens_id=None,
            )
        except ReviewDecisionReferenceError as exc:
            error = exc
        except IntegrityError as exc:
            _update_pipeline(
                owner,
                change_id=change_id,
                review_id=pack["review_id"],
                operation_id=root_operation_id,
                stage=lane_key,
                status="failed",
                failure_reason=str(exc),
                failure_kind="invalid-repair-output",
            )
            raise IntegrityError(
                f"Semantic output is not eligible for bounded repair: {exc}"
            ) from exc
        else:
            raise IntegrityError("Invalid semantic output has no reference error")
        original_entry = entry
    else:
        original_entry, raw_decision, error = historical
    return _execute_decision_repair(
        owner,
        change_id=change_id,
        state=StateStore(owner).load(change_id),
        pack=pack,
        context_path=context_path,
        root_operation_id=root_operation_id,
        source_lane_key=lane_key,
        original_entry=original_entry,
        raw_decision=raw_decision,
        error=error,
        effort=effort,
        stream_callback=stream_callback,
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
            "normalized_output_path",
            "normalized_output_digest",
            "identifier_normalizations",
            "transcript_path",
            "transcript_digest",
            "source_snapshot_digest",
            "repair_contract",
            "repair_source_lane_key",
            "repair_original_attempt_id",
            "repair_original_output_digest",
            "repair_error_code",
            "repair_error_digest",
            "repair_reserved_ids",
            "input_bundle_digest",
            "input_bundle_bytes",
            "budget_contract",
            "budget",
            "budget_status",
            "budget_warning",
            "budget_recovery_contract",
            "recovered_budget",
            "recovered_without_model_call",
            "original_budget_failure_reason",
            "final_coverage_contract",
            "final_coverage_digest",
            "final_coverage_path_count",
            "final_patch_digest",
            "final_patch_bytes",
            "final_input_bundle_digest",
            "final_input_bundle_bytes",
            "bound_context_contract",
            "bound_context_digest",
            "bound_context_input_count",
            "bound_context_bytes",
            "workspace_mode",
        )
    }


def _repair_provenance(entry: dict[str, Any]) -> dict[str, Any] | None:
    if entry.get("repair_contract") != REVIEW_DECISION_REPAIR_CONTRACT:
        return None
    return {
        "contract": REVIEW_DECISION_REPAIR_CONTRACT,
        "source_lane_key": entry.get("repair_source_lane_key"),
        "original_attempt_id": entry.get("repair_original_attempt_id"),
        "original_output_digest": entry.get("repair_original_output_digest"),
        "error_code": entry.get("repair_error_code"),
        "error_digest": entry.get("repair_error_digest"),
        "input_bundle_digest": entry.get("input_bundle_digest"),
        "repair_attempt_id": entry.get("attempt_id"),
        "repair_output_digest": entry.get("output_digest"),
        "model": entry.get("model"),
        "reasoning_effort": entry.get("reasoning_effort"),
        "started_at": entry.get("started_at"),
        "completed_at": entry.get("completed_at"),
        "transcript_digest": entry.get("transcript_digest"),
    }


def _has_actionable_review_finding(decision: dict[str, Any]) -> bool:
    return any(
        finding.get("severity") in {"blocker", "should-fix"}
        and "review" in _finding_blocks(finding)
        for finding in decision.get("findings", [])
        if isinstance(finding, dict)
    )


def _native_review_is_clean(output: bytes) -> bool:
    text = output.decode("utf-8", errors="replace").strip().lower()
    if not text:
        return True
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("findings"), list):
        return not any(
            isinstance(item, dict)
            and item.get("severity") in {"blocker", "should-fix"}
            for item in payload["findings"]
        )
    try:
        projection = _native_plaintext_projection(
            output.decode("utf-8", errors="replace")
        )
    except IntegrityError:
        return False
    return not any(
        isinstance(item, dict)
        and item.get("severity") in {"blocker", "should-fix"}
        for item in projection.get("findings", [])
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
    failure_kind: str | None = None,
    task_context: dict[str, Any] | None = None,
    pipeline_instance_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    updates: dict[str, Any] = {
        "status": status,
        "stage": stage,
        "runner_pid": os.getpid(),
        "updated_at": utc_now(),
    }
    if failure_reason is not None:
        updates["failure_reason"] = failure_reason
    if failure_kind is not None:
        updates["failure_kind"] = failure_kind
    if task_context is not None:
        updates["task_context"] = copy.deepcopy(task_context)
    if pipeline_instance_id is not None:
        updates["pipeline_instance_id"] = pipeline_instance_id
    if status in {"completed", "failed", "failed-finalize"}:
        updates["completed_at"] = utc_now()
    for lock_attempt in range(21):
        try:
            result = StateStore(owner).update_review_pipeline(
                change_id,
                review_id=review_id,
                operation_id=operation_id,
                updates=updates,
                create=create,
            )
            return result
        except LockError:
            if lock_attempt == 20:
                raise
            time.sleep(0.05)


def _build_review_ir(
    *,
    owner: Path,
    pack: dict[str, Any],
    start_result: dict[str, Any],
    decision: dict[str, Any],
    independent_entry: dict[str, Any],
    reconciliation_entry: dict[str, Any] | None,
    specialist_entries: list[tuple[dict[str, Any], str]],
    final_full_entry: dict[str, Any] | None,
    identifier_normalizations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    semantic_context_path = safe_resolve(
        owner,
        independent_entry["context_manifest_path"],
        must_exist=True,
    )
    if sha256_file(semantic_context_path) != independent_entry.get("context_digest"):
        raise IntegrityError("Semantic context digest changed before ReviewIR assembly")
    semantic_context = read_json(semantic_context_path)
    semantic_context_manifest_digest = semantic_context.get("manifest_digest")
    if not isinstance(semantic_context_manifest_digest, str):
        raise IntegrityError("Semantic context is missing its manifest digest")
    decision, derived_normalizations = _normalize_structured_payload(
        decision,
        pack=pack,
        payload_kind="decision",
        lens_id=None,
    )
    if identifier_normalizations is None:
        identifier_normalizations = derived_normalizations
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
            **{
                key: independent_entry.get(key)
                for key in (
                    "budget_contract",
                    "budget",
                    "budget_status",
                    "budget_warning",
                    "budget_recovery_contract",
                    "recovered_budget",
                    "recovered_without_model_call",
                    "original_budget_failure_reason",
                )
                if independent_entry.get(key) is not None
            },
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
                **{
                    key: final_full_entry.get(key)
                    for key in (
                        "budget_contract",
                        "budget",
                        "budget_status",
                        "budget_warning",
                        "command_budget_contract",
                        "command_target",
                        "command_ceiling",
                        "budget_recovery_contract",
                        "recovered_budget",
                        "recovered_without_model_call",
                        "original_budget_failure_reason",
                        "final_coverage_contract",
                        "final_coverage_digest",
                        "final_coverage_path_count",
                        "final_patch_digest",
                        "final_patch_bytes",
                        "final_input_bundle_digest",
                        "final_input_bundle_bytes",
                        "bound_context_contract",
                        "bound_context_digest",
                        "bound_context_input_count",
                        "bound_context_bytes",
                        "workspace_mode",
                    )
                    if final_full_entry.get(key) is not None
                },
            }
        )
    repairs = [
        repair
        for entry in (
            independent_entry,
            reconciliation_entry,
            final_full_entry,
        )
        if entry is not None
        if (repair := _repair_provenance(entry)) is not None
    ]
    lanes: dict[str, Any] = {
        "semantic": {
            "status": "completed",
            "model": independent_entry["model"],
            "reasoning_effort": independent_entry["reasoning_effort"],
            "context_manifest_path": independent_entry["context_manifest_path"],
            "context_manifest_digest": semantic_context_manifest_digest,
            "independent_draft_path": independent_entry["output_path"],
            "independent_draft_digest": independent_entry["output_digest"],
            "attempt_id": independent_entry["attempt_id"],
            "operation_id": independent_entry["operation_id"],
            "transcript_path": independent_entry["transcript_path"],
            "transcript_digest": independent_entry["transcript_digest"],
            "passes": passes,
            **({"repairs": repairs} if repairs else {}),
        },
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
    if reconciliation_entry is not None:
        lanes["reconciliation"] = _lane_provenance(reconciliation_entry)
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
            "normalized_output_path": native.get("normalized_output_path"),
            "normalized_output_digest": native.get("normalized_output_digest"),
            "native_output_format": native.get("native_output_format"),
            "native_decision_status": native.get("native_decision_status"),
            "native_plaintext_projection_contract": native.get(
                "native_plaintext_projection_contract"
            ),
            "native_transcript_validation_contract": native.get(
                "native_transcript_validation_contract"
            ),
            "transcript_path": native.get("transcript_path"),
            "transcript_digest": native.get("transcript_digest"),
            "source_snapshot_digest": native["source_snapshot_digest"],
            "native_workspace_contract": native.get("native_workspace_contract"),
            "workspace_isolation": native.get("workspace_isolation"),
            "workspace_head_sha": native.get("workspace_head_sha"),
            "workspace_source_snapshot_before": native.get(
                "workspace_source_snapshot_before"
            ),
            "workspace_source_snapshot_after": native.get(
                "workspace_source_snapshot_after"
            ),
            "coverage_chain": start_result.get("native_coverage", []),
        }
    return {
        "schema_version": 2,
        "runner_contract": pack.get("runner_contract", REVIEW_RUNNER_CONTRACT),
        **({"context_contract": pack["context_contract"]} if pack.get("context_contract") else {}),
        **({"economy_contract": pack["economy_contract"]} if pack.get("economy_contract") else {}),
        **({"budget_contract": pack["budget_contract"]} if pack.get("budget_contract") else {}),
        **({"native_output_contract": pack["native_output_contract"]} if pack.get("native_output_contract") else {}),
        **(
            {"native_workspace_contract": pack["native_workspace_contract"]}
            if pack.get("native_workspace_contract")
            else {}
        ),
        **(
            {"decision_repair_contract": REVIEW_DECISION_REPAIR_CONTRACT}
            if repairs
            else {}
        ),
        **(
            {"identifier_contract": pack["identifier_contract"]}
            if pack.get("identifier_contract") == REVIEW_IDENTIFIER_CONTRACT
            else {}
        ),
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
        "identifier_normalizations": identifier_normalizations,
    }


def _completed_lane_entry(
    state: dict[str, Any],
    *,
    review_id: str,
    lane_key: str,
) -> dict[str, Any]:
    entry = next(
        (
            item
            for item in reversed(state["reviews"])
            if isinstance(item, dict)
            and item.get("review_id") == review_id
            and item.get("lane_key") == lane_key
            and item.get("status") == "completed"
        ),
        None,
    )
    if entry is None:
        raise IntegrityError(
            f"Failed-finalize recovery is missing completed lane: {lane_key}"
        )
    return entry


def _finalize_review(
    owner: Path,
    *,
    change_id: str,
    pack: dict[str, Any],
    start_result: dict[str, Any],
    decision: dict[str, Any],
    independent_entry: dict[str, Any],
    reconciliation_entry: dict[str, Any] | None,
    specialist_entries: list[tuple[dict[str, Any], str]],
    final_full_entry: dict[str, Any] | None,
    pipeline_operation_id: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    _update_pipeline(
        owner,
        change_id=change_id,
        review_id=pack["review_id"],
        operation_id=pipeline_operation_id,
        stage="finalizing",
        create=True,
    )
    try:
        terminal_entry = final_full_entry or reconciliation_entry or independent_entry
        terminal_decision = _completed_lane_payload(
            owner,
            terminal_entry,
            pack=pack,
            payload_kind="decision",
            lens_id=None,
        )
        if terminal_decision != decision:
            raise IntegrityError("Terminal review decision projection mismatch")
        identifier_normalizations = terminal_entry.get(
            "identifier_normalizations", []
        )
        report = _build_review_ir(
            owner=owner,
            pack=pack,
            start_result=start_result,
            decision=decision,
            independent_entry=independent_entry,
            reconciliation_entry=reconciliation_entry,
            specialist_entries=specialist_entries,
            final_full_entry=final_full_entry,
            identifier_normalizations=identifier_normalizations,
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
        failure_kind = (
            "model-output"
            if isinstance(exc, ReviewDecisionReferenceError)
            else "deterministic-finalization"
        )
        _update_pipeline(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
            operation_id=pipeline_operation_id,
            stage="finalizing",
            status="failed-finalize",
            failure_reason=f"{type(exc).__name__}: {exc}",
            failure_kind=failure_kind,
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
    return report, imported, build_review_presentation(owner, report)


def _resume_failed_finalization(
    owner: Path,
    *,
    change_id: str,
    review_id: str,
    effective_operation_id: str,
    owner_selection: str,
) -> dict[str, Any]:
    """Reassemble an exact-HEAD review without recomputing lane contracts."""

    state = StateStore(owner).load(change_id)
    relative_pack_path, pack = _pack_for_review(owner, state, review_id)
    _validate_review_pack_current(owner, state=state, pack=pack)
    independent_key = (
        "native"
        if pack.get("control_level") == "routine"
        else ("semantic:full" if pack["review_mode"] == "full" else "semantic:targeted")
    )
    independent_entry = _completed_lane_entry(
        state,
        review_id=review_id,
        lane_key=independent_key,
    )
    terminal_reference_error: ReviewDecisionReferenceError | None = None
    independent_decision: dict[str, Any] | None = None
    try:
        independent_decision = _completed_lane_payload(
            owner,
            independent_entry,
            pack=pack,
            payload_kind="decision",
            lens_id=None,
        )
    except ReviewDecisionReferenceError as exc:
        terminal_reference_error = exc
    reconciliation_entry = next(
        (
            item
            for item in reversed(state["reviews"])
            if isinstance(item, dict)
            and item.get("review_id") == review_id
            and item.get("lane_key") == "reconciliation"
            and item.get("status") == "completed"
        ),
        None,
    )
    if reconciliation_entry is None and pack.get("runner_contract") != REVIEW_RUNNER_CONTRACT:
        reconciliation_entry = _completed_lane_entry(
            state,
            review_id=review_id,
            lane_key="reconciliation",
        )
    reconciliation_decision: dict[str, Any] | None = None
    if reconciliation_entry is not None:
        try:
            reconciliation_decision = _completed_lane_payload(
                owner,
                reconciliation_entry,
                pack=pack,
                payload_kind="decision",
                lens_id=None,
            )
        except ReviewDecisionReferenceError as exc:
            terminal_reference_error = exc
    decision = reconciliation_decision or independent_decision
    specialist_entries: list[tuple[dict[str, Any], str]] = []
    specialist_payloads: dict[str, bytes] = {}
    for lens in pack.get("risk_lenses", []):
        entry = _completed_lane_entry(
            state,
            review_id=review_id,
            lane_key=f"specialist:{lens['id']}",
        )
        specialist_payload = _completed_lane_payload(
            owner,
            entry,
            pack=pack,
            payload_kind="specialist",
            lens_id=lens["id"],
        )
        specialist_entries.append((entry, lens["id"]))
        specialist_payloads[f"{lens['id']}.json"] = json.dumps(
            specialist_payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    final_full_entry = next(
        (
            item
            for item in reversed(state["reviews"])
            if isinstance(item, dict)
            and item.get("review_id") == review_id
            and item.get("lane_key") == "semantic:final-full"
            and item.get("status") == "completed"
        ),
        None,
    )
    if final_full_entry is not None:
        try:
            decision = _completed_lane_payload(
                owner,
                final_full_entry,
                pack=pack,
                payload_kind="decision",
                lens_id=None,
            )
            terminal_reference_error = None
        except ReviewDecisionReferenceError as exc:
            decision = None
            terminal_reference_error = exc
    native_entry = next(
        (
            item
            for item in reversed(state["reviews"])
            if isinstance(item, dict)
            and item.get("review_id") == review_id
            and item.get("lane_key") == "native"
            and item.get("status") == "completed"
        ),
        None,
    )
    native_coverage = list(pack.get("prior_native_coverage", []))
    if native_entry is not None:
        output_path = safe_resolve(
            owner,
            native_entry["output_path"],
            must_exist=True,
        )
        if sha256_file(output_path) != native_entry.get("output_digest"):
            raise IntegrityError("Completed native output digest mismatch")
        native_coverage.append(
            {
                "review_id": review_id,
                "base_sha": pack["comparison_base_sha"],
                "head_sha": pack["head_sha"],
                "output_digest": native_entry["output_digest"],
            }
        )
    native_bytes = b"No native lane was required for this ReviewPack.\n"
    if native_entry is not None:
        native_bytes = safe_resolve(
            owner,
            native_entry["output_path"],
            must_exist=True,
        ).read_bytes()
    context_path = independent_entry.get("context_manifest_path")
    if not isinstance(context_path, str):
        raise IntegrityError("Completed semantic lane is missing context manifest")
    resolved_context = safe_resolve(owner, context_path, must_exist=True)
    if sha256_file(resolved_context) != independent_entry.get("context_digest"):
        raise IntegrityError("Completed semantic context digest mismatch")
    context_manifest = read_json(resolved_context)
    context_manifest_digest = context_manifest.get("manifest_digest")
    if not isinstance(context_manifest_digest, str) or not context_manifest_digest:
        raise IntegrityError("Completed semantic context manifest is missing digest")
    start_result = {
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "review_id": review_id,
        "review_pack_path": relative_pack_path,
        "pack_created": False,
        "review_context_path": context_path,
        "review_context_digest": context_manifest_digest,
        "native": native_entry,
        "native_coverage": native_coverage,
    }
    pipeline_operation_id = f"{effective_operation_id}:{review_id}"
    if decision is None:
        if terminal_reference_error is None:
            raise IntegrityError("Failed-finalize recovery has no terminal decision")
        source_entry = final_full_entry or reconciliation_entry or independent_entry
        source_lane_key = (
            "semantic:final-full"
            if final_full_entry is not None
            else (
                "reconciliation"
                if reconciliation_entry is not None
                else independent_key
            )
        )
        raw_decision = read_json(
            safe_resolve(owner, source_entry["output_path"], must_exist=True)
        )
        correction_entry, corrected_decision = _execute_decision_repair(
            owner,
            change_id=change_id,
            state=state,
            pack=pack,
            context_path=context_path,
            root_operation_id=pipeline_operation_id,
            source_lane_key=source_lane_key,
            original_entry=source_entry,
            raw_decision=raw_decision,
            error=terminal_reference_error,
            effort=_semantic_review_effort(state),
        )
        if corrected_decision is None:
            return {
                **review_status(owner, change_id=change_id, review_id=review_id),
                "operation_id": effective_operation_id,
                "review_result_path": None,
            }
        decision = corrected_decision
        if final_full_entry is not None:
            final_full_entry = correction_entry
        else:
            reconciliation_entry = correction_entry
    report, imported, presentation = _finalize_review(
        owner,
        change_id=change_id,
        pack=pack,
        start_result=start_result,
        decision=decision,
        independent_entry=independent_entry,
        reconciliation_entry=reconciliation_entry,
        specialist_entries=specialist_entries,
        final_full_entry=final_full_entry,
        pipeline_operation_id=pipeline_operation_id,
    )
    result = {
        "ok": imported["review_result_path"] is not None,
        "dry_run": False,
        "changed": imported["changed"],
        "status": "completed",
        "change_id": change_id,
        "state_revision": imported["state_revision"],
        "operation_id": effective_operation_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "review_id": review_id,
        "review_pack_path": relative_pack_path,
        "pack_created": False,
        "runner_contract": pack.get("runner_contract", REVIEW_RUNNER_CONTRACT),
        "verdict": imported["verdict"],
        "finding_counts": imported["finding_counts"],
        "review_result_path": imported["review_result_path"],
        "remediation_manifest_path": imported.get("remediation_manifest_path"),
        "presentation": presentation,
        "next_action": imported["next_action"],
        "reused_completed_lanes": True,
        "identifier_normalizations": report.get("identifier_normalizations", []),
    }
    result["delivery_receipt"] = imported["delivery_receipt"]
    return result


def review_run(
    root: Path,
    *,
    change_id: str,
    pack_path: str | None,
    operation_id: str | None,
    dry_run: bool = False,
    stream_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    handoff_recovered = False
    recovered_candidate_run_id: str | None = None
    recovered_pack_created = False
    task_context = unavailable_task_context("review")

    def annotate_handoff(payload: dict[str, Any]) -> dict[str, Any]:
        payload["handoff_recovered"] = handoff_recovered
        payload["candidate_run_id"] = recovered_candidate_run_id
        if recovered_pack_created:
            payload["pack_created"] = True
        payload["task_context"] = task_context
        return payload

    def emit(event: str, **values: Any) -> None:
        if stream_callback is None:
            return
        payload = {"event": event, "change_id": change_id, **values}
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if event == "heartbeat" and len(encoded.encode("utf-8")) > 512:
            payload = {
                "event": "heartbeat",
                "change_id": change_id,
                "review_id": values.get("review_id"),
                "lane": values.get("lane"),
                "elapsed_seconds": values.get("elapsed_seconds"),
            }
        stream_callback(payload)

    def incomplete_result(owner: Path, review_id: str) -> dict[str, Any]:
        pending = {
            **review_status(owner, change_id=change_id, review_id=review_id),
            "operation_id": effective_operation_id,
            "review_result_path": None,
        }
        next_action = pending.get("next_action", {})
        if next_action.get("id") == "inspect-review-budget":
            emit(
                "budget-warning",
                review_id=review_id,
                detail=next_action.get("detail"),
            )
        emit(
            "completed",
            review_id=review_id,
            status=pending.get("status"),
            next_action=next_action.get("id"),
        )
        return annotate_handoff(pending)

    effective_operation_id = operation_id or str(uuid.uuid4())
    pipeline_instance_id = str(uuid.uuid4())
    emit("started", operation_id=effective_operation_id)
    explicit_pack = Path(pack_path).is_absolute() if pack_path else False
    if not explicit_pack:
        # Check the state-owned single-flight lease before assembling the more
        # expensive status projection. A short native lane must not finish in
        # the time spent building telemetry and let a concurrent caller enter
        # finalization with a different root operation ID.
        quick_candidate = root.resolve()
        if (
            (quick_candidate / ".dls" / "config.toml").is_file()
            and StateStore(quick_candidate).path(change_id).is_file()
            and not registry_routes_change_elsewhere(quick_candidate, change_id)
        ):
            # This fast read is allowed only when the caller is the canonical
            # owner. Registry validation can be slow enough for very short
            # lanes to finish before a duplicate observes their lease, while a
            # portable state copy must never hide the registered owner.
            quick_owner = quick_candidate
            quick_owner_selection = "current-checkout"
        else:
            quick_owner, quick_owner_selection = _owner_root(root, change_id)
        quick_state = StateStore(quick_owner).load(change_id)
        active_attempt = next(
            (
                item
                for item in reversed(quick_state.get("reviews", []))
                if isinstance(item, dict)
                and item.get("status") == "running"
                and isinstance(item.get("review_id"), str)
                and _process_is_alive(item.get("runner_pid"))
            ),
            None,
        )
        if active_attempt is not None:
            active_review_id = active_attempt["review_id"]
            active_pack = next(
                (
                    item
                    for item in reversed(quick_state.get("reviews", []))
                    if isinstance(item, dict)
                    and item.get("kind") == "pack"
                    and item.get("review_id") == active_review_id
                ),
                None,
            )
            quick_head = git_head(quick_owner)
            active_head = (
                active_pack.get("head_sha") if isinstance(active_pack, dict) else None
            )
            exact_head = bool(active_head and active_head == quick_head)
            running = {
                "ok": True,
                "changed": False,
                "dry_run": dry_run,
                "change_id": change_id,
                "state_revision": quick_state["state_revision"],
                "operation_id": effective_operation_id,
                "owner_root": str(quick_owner),
                "owner_selection": quick_owner_selection,
                "current_head": quick_head,
                "candidate_head": active_head,
                "exact_head": exact_head,
                "prepared": exact_head,
                "review_id": active_review_id,
                "status": "running",
                "verdict": None,
                "review_result_path": None,
                "remediation_manifest_path": None,
                "review_pack_path": (
                    active_pack.get("pack_path")
                    if isinstance(active_pack, dict)
                    else None
                ),
                "pack_created": False,
                "next_action": {
                    "id": "wait-review",
                    "detail": "review pipeline is active",
                },
            }
            emit(
                "completed",
                review_id=active_review_id,
                status="running",
                next_action="wait-review",
            )
            return annotate_handoff(running)
        existing_status = review_status(root, change_id=change_id)
        task_context = existing_status.get("task_context") or task_context
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
            existing_status["delivery_receipt"] = delivery_receipt(
                Path(existing_status["owner_root"]), change_id=change_id
            )
            emit(
                "completed",
                review_id=existing_status.get("review_id"),
                status="completed",
                verdict=existing_status.get("verdict"),
                review_result_path=existing_status.get("review_result_path"),
                next_action=existing_status.get("next_action", {}).get("id"),
            )
            return annotate_handoff(existing_status)
        if existing_status.get("status") == "running":
            running_owner = Path(existing_status["owner_root"])
            running_state = StateStore(running_owner).load(change_id)
            execution_is_live = any(
                isinstance(item, dict)
                and item.get("review_id") == existing_status.get("review_id")
                and item.get("status") == "running"
                and _process_is_alive(item.get("runner_pid"))
                for item in running_state.get("reviews", [])
            )
            if execution_is_live:
                existing_status.update(
                    {
                        "dry_run": dry_run,
                        "operation_id": effective_operation_id,
                        "review_pack_path": existing_status.get("review_pack_path"),
                        "pack_created": False,
                        "review_result_path": None,
                    }
                )
                emit(
                    "completed",
                    review_id=existing_status.get("review_id"),
                    status="running",
                    next_action="wait-review",
                )
                return annotate_handoff(existing_status)
        if existing_status.get("status") == "preparing-candidate":
            existing_status.update(
                {
                    "dry_run": dry_run,
                    "operation_id": effective_operation_id,
                    "review_pack_path": None,
                    "pack_created": False,
                    "review_result_path": None,
                }
            )
            emit(
                "completed",
                review_id=None,
                status="preparing-candidate",
                next_action="wait-review",
            )
            return annotate_handoff(existing_status)
        if (
            existing_status["status"] == "failed-finalize"
            and isinstance(existing_status.get("review_id"), str)
        ):
            if dry_run:
                existing_status.update(
                    {
                        "dry_run": True,
                        "operation_id": effective_operation_id,
                        "review_pack_path": None,
                        "pack_created": False,
                        "reused_completed_lanes": True,
                    }
                )
                return annotate_handoff(existing_status)
            recovery_owner = Path(existing_status["owner_root"])
            recovery_review_id = existing_status["review_id"]
            try:
                recovered = _resume_failed_finalization(
                    recovery_owner,
                    change_id=change_id,
                    review_id=recovery_review_id,
                    effective_operation_id=effective_operation_id,
                    owner_selection=existing_status["owner_selection"],
                )
                emit(
                    "delivery-receipt",
                    review_id=recovery_review_id,
                    delivery_receipt=recovered["delivery_receipt"],
                )
                emit(
                    "completed",
                    review_id=recovery_review_id,
                    status=recovered.get("status"),
                    verdict=recovered.get("verdict"),
                    review_result_path=recovered.get("review_result_path"),
                    next_action=recovered.get("next_action", {}).get("id"),
                )
                return annotate_handoff(recovered)
            except IntegrityError as exc:
                recovery_operation_id = (
                    f"{effective_operation_id}:{recovery_review_id}"
                )
                recovery_state = StateStore(recovery_owner).load(change_id)
                pipeline = next(
                    (
                        item
                        for item in reversed(recovery_state["reviews"])
                        if isinstance(item, dict)
                        and item.get("kind") == "pipeline"
                        and item.get("review_id") == recovery_review_id
                        and item.get("operation_id") == recovery_operation_id
                    ),
                    None,
                )
                if pipeline is None or not pipeline.get("failure_kind"):
                    _update_pipeline(
                        recovery_owner,
                        change_id=change_id,
                        review_id=recovery_review_id,
                        operation_id=recovery_operation_id,
                        stage="finalizing",
                        status="failed-finalize",
                        create=True,
                        failure_reason=f"{type(exc).__name__}: {exc}",
                        failure_kind="integrity",
                    )
                raise
        if existing_status.get("next_action", {}).get("id") in {
            "prepare-candidate",
            "recover-remediation-manifest",
        }:
            recovery_owner = Path(existing_status["owner_root"])
            can_recover_handoff = (
                existing_status.get("next_action", {}).get("id") == "prepare-candidate"
                and isinstance(existing_status.get("prior_review_id"), str)
                and StateStore(recovery_owner).load(change_id).get("control_level")
                in {"standard", "critical"}
            )
            if can_recover_handoff:
                from .candidate_runner import candidate_ready
                emit(
                    "candidate-transition",
                    review_id=None,
                    phase="preflight",
                )
                emit(
                    "candidate-transition",
                    review_id=None,
                    phase="validating",
                )
                candidate = candidate_ready(
                    recovery_owner,
                    change_id=change_id,
                    base_ref=None,
                    addressed=[],
                    noted=[],
                    extra_commands=[],
                    operation_id=None,
                    dry_run=dry_run,
                    _bind_task_context=False,
                )
                recovered_candidate_run_id = candidate.get("run_id")
                if candidate.get("status") == "completed" and isinstance(
                    candidate.get("review_pack_path"), str
                ):
                    handoff_recovered = True
                    recovered_pack_created = True
                    if not dry_run and isinstance(recovered_candidate_run_id, str):
                        StateStore(recovery_owner).update_candidate_run(
                            change_id,
                            run_id=recovered_candidate_run_id,
                            updates={"handoff_recovered_by_review": True},
                        )
                    emit(
                        "candidate-transition",
                        review_id=candidate.get("review_id"),
                        phase="prepared",
                    )
                    pack_path = str(
                        safe_resolve(
                            recovery_owner,
                            candidate["review_pack_path"],
                            must_exist=not dry_run,
                        )
                    )
                    root = recovery_owner
                else:
                    action = candidate.get("next_action") or {
                        "id": "wait-review",
                        "detail": "candidate preparation is active",
                    }
                    status_value = (
                        "preparing-candidate"
                        if candidate.get("status") == "running"
                        else candidate.get("status", "blocked")
                    )
                    blocked = {
                        **existing_status,
                        "dry_run": dry_run,
                        "operation_id": effective_operation_id,
                        "status": status_value,
                        "review_pack_path": None,
                        "pack_created": False,
                        "review_result_path": None,
                        "verdict": None,
                        "presentation": None,
                        "next_action": (
                            {"id": "wait-review", "detail": action.get("detail", "")}
                            if status_value == "preparing-candidate"
                            else action
                        ),
                    }
                    emit(
                        "completed",
                        review_id=None,
                        status=status_value,
                        next_action=blocked["next_action"].get("id"),
                    )
                    return annotate_handoff(blocked)
            else:
                existing_status.update(
                    {
                        "dry_run": dry_run,
                        "operation_id": effective_operation_id,
                        "review_pack_path": None,
                        "pack_created": False,
                        "review_result_path": None,
                        "verdict": None,
                        "presentation": None,
                    }
                )
                emit(
                    "completed",
                    review_id=existing_status.get("review_id"),
                    status=existing_status.get("status"),
                    next_action=existing_status.get("next_action", {}).get("id"),
                )
                return annotate_handoff(existing_status)
        if (
            existing_status.get("status") == "not-prepared"
            and not recovered_pack_created
        ):
            # Decision and other typed readiness boundaries must be reported
            # before resolving a ReviewPack.  In particular, a review task is
            # not allowed to turn an outstanding human decision into a
            # missing-pack integrity failure or to prepare the candidate on
            # the user's behalf.
            existing_status.update(
                {
                    "dry_run": dry_run,
                    "operation_id": effective_operation_id,
                    "review_id": None,
                    "review_pack_path": None,
                    "pack_created": False,
                    "review_result_path": None,
                    "remediation_manifest_path": None,
                    "verdict": None,
                    "presentation": None,
                }
            )
            emit(
                "completed",
                review_id=None,
                status="not-prepared",
                next_action=existing_status.get("next_action", {}).get("id"),
            )
            return annotate_handoff(existing_status)
    context_owner, _, context_pack, _, _ = _resolve_review_pack(
        root,
        change_id=change_id,
        pack_path=pack_path,
        allow_missing_current=False,
    )
    task_context = review_task_context(
        context_owner,
        change_id=change_id,
        operation_id=effective_operation_id,
        review_id=context_pack.get("review_id"),
        pack_digest=context_pack.get("pack_digest"),
        record=not dry_run,
        allow_cross_role=context_pack.get("control_level") == "routine",
    )
    if task_context.get("status") == "reused":
        emit(
            "context-warning",
            review_id=context_pack.get("review_id"),
            recommendation="open-fresh-task",
            reuse_reason=task_context.get("reuse_reason"),
        )
    pipeline_operation_id = (
        f"{effective_operation_id}:{context_pack['review_id']}"
    )
    if not dry_run:
        _, active_pipeline, claimed_pipeline = _update_pipeline(
            context_owner,
            change_id=change_id,
            review_id=context_pack["review_id"],
            operation_id=pipeline_operation_id,
            stage="native",
            create=True,
            task_context=task_context,
            pipeline_instance_id=pipeline_instance_id,
        )
        if not claimed_pipeline:
            running = review_status(
                context_owner,
                change_id=change_id,
                review_id=context_pack["review_id"],
            )
            running.update(
                {
                    "dry_run": False,
                    "operation_id": effective_operation_id,
                    "review_result_path": None,
                    "next_action": {
                        "id": "wait-review",
                        "detail": "review pipeline is active",
                    },
                }
            )
            emit(
                "completed",
                review_id=context_pack["review_id"],
                status="running",
                next_action="wait-review",
            )
            return annotate_handoff(running)
    if not dry_run:
        record_review_task_reference(
            context_owner,
            change_id=change_id,
            review_id=context_pack["review_id"],
            operation_id=effective_operation_id,
            task_context=task_context,
        )
    try:
        started = review_start(
            root,
            change_id=change_id,
            pack_path=pack_path,
            operation_id=f"{effective_operation_id}:native",
            dry_run=dry_run,
            stream_callback=(lambda event: emit(event.pop("event"), **event)) if stream_callback else None,
        )
    except Exception as exc:
        if not dry_run:
            _update_pipeline(
                context_owner,
                change_id=change_id,
                review_id=context_pack["review_id"],
                operation_id=pipeline_operation_id,
                stage="native",
                status="failed",
                failure_reason=f"{type(exc).__name__}: {exc}",
                failure_kind="native-start",
            )
        raise
    if not started.get("ok") or started.get("status") in {"running", "failed"}:
        if not dry_run:
            _update_pipeline(
                context_owner,
                change_id=change_id,
                review_id=context_pack["review_id"],
                operation_id=pipeline_operation_id,
                stage="native",
                status="failed",
                failure_reason=(
                    started.get("next_action", {}).get("detail")
                    or f"native review returned {started.get('status')}"
                ),
                failure_kind="native-start",
            )
        if isinstance(started.get("review_id"), str) and isinstance(
            started.get("owner_root"), str
        ):
            record_review_task_reference(
                Path(started["owner_root"]),
                change_id=change_id,
                review_id=started["review_id"],
                operation_id=effective_operation_id,
                task_context=task_context,
            )
        started["review_result_path"] = None
        if started.get("next_action", {}).get("id") == "inspect-review-budget":
            emit(
                "budget-warning",
                review_id=started.get("review_id"),
                detail=started["next_action"].get("detail"),
            )
        emit(
            "completed",
            review_id=started.get("review_id"),
            status=started.get("status"),
            next_action=started.get("next_action", {}).get("id"),
        )
        return annotate_handoff(started)
    if dry_run:
        started.update(
            {
                "status": "ready",
                "runner_contract": started.get("runner_contract", REVIEW_RUNNER_CONTRACT),
                "projected_lanes": {
                    "native": started["native_required"],
                    "specialists": (
                        [item["id"] for item in started["risk_lenses"]]
                        if started["review_mode"] == "full"
                        else []
                    ),
                    "semantic": (
                        "routine-terra"
                        if started.get("control_level") == "routine"
                        else (
                            "full"
                            if started["review_mode"] == "full"
                            else "targeted"
                        )
                    ),
                    "reconciliation": "conditional",
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
        emit(
            "completed",
            review_id=started.get("review_id"),
            status="ready",
            next_action="start-review-run",
        )
        return annotate_handoff(started)
    owner = Path(started["owner_root"])
    state = StateStore(owner).load(change_id)
    relative_pack_path, pack = _pack_for_review(
        owner,
        state,
        started["review_id"],
    )
    pipeline_operation_id = f"{effective_operation_id}:{pack['review_id']}"
    record_review_task_reference(
        owner,
        change_id=change_id,
        review_id=pack["review_id"],
        operation_id=effective_operation_id,
        task_context=task_context,
    )
    _validate_review_pack_current(owner, state=state, pack=pack)
    context_path = started["review_context_path"]
    semantic_effort = _semantic_review_effort(state)
    prompt_values = _review_prompt_values(pack)
    selected_risk_lenses = (
        pack.get("risk_lenses", []) if pack["review_mode"] == "full" else []
    )
    _update_pipeline(
        owner,
        change_id=change_id,
        review_id=pack["review_id"],
        operation_id=pipeline_operation_id,
        stage="specialists" if selected_risk_lenses else "semantic-independent",
        create=True,
        task_context=task_context,
    )
    specialist_entries: list[tuple[dict[str, Any], str]] = []
    specialist_payloads: dict[str, bytes] = {}
    for lens in selected_risk_lenses:
        emit("lane-transition", review_id=pack["review_id"], lane=f"specialist:{lens['id']}")
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
            stream_callback=(lambda event: emit(event.pop("event"), **event)) if stream_callback else None,
        )
        if payload is None:
            return incomplete_result(owner, pack["review_id"])
        specialist_entries.append((entry, lens["id"]))
        specialist_payloads[f"{lens['id']}.json"] = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    independent_kind = (
        "full" if pack["review_mode"] == "full" else "targeted"
    )
    native = started.get("native")
    if pack["control_level"] == "routine":
        if not isinstance(native, dict) or not isinstance(native.get("output_path"), str):
            raise IntegrityError("Routine review is missing its Terra review attempt")
        independent_entry = native
        independent_decision = _completed_lane_payload(
            owner,
            native,
            pack=pack,
            payload_kind="decision",
            lens_id=None,
        )
        _update_pipeline(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
            operation_id=pipeline_operation_id,
            stage="routine:terra",
        )
    else:
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
        emit(
            "lane-transition",
            review_id=pack["review_id"],
            lane=f"semantic:{independent_kind}",
        )
        independent_entry, independent_decision = _execute_decision_lane(
            owner,
            change_id=change_id,
            pack=pack,
            context_path=context_path,
            root_operation_id=pipeline_operation_id,
            lane_key=f"semantic:{independent_kind}",
            lane_kind="semantic",
            effort=semantic_effort,
            prompt_text=independent_prompt,
            model=SEMANTIC_MODEL,
            stream_callback=(lambda event: emit(event.pop("event"), **event)) if stream_callback else None,
        )
        if independent_decision is None:
            return incomplete_result(owner, pack["review_id"])
    native_bytes = b"No native lane was required for this ReviewPack.\n"
    if native and isinstance(native.get("output_path"), str):
        native_bytes = safe_resolve(
            owner,
            native["output_path"],
            must_exist=True,
        ).read_bytes()
    specialist_actionable = any(
        _has_actionable_review_finding(payload)
        for payload in (
            json.loads(value.decode("utf-8")) for value in specialist_payloads.values()
        )
    )
    needs_reconciliation = (
        pack["control_level"] != "routine"
        and (
            _has_actionable_review_finding(independent_decision)
            or specialist_actionable
            or not _native_review_is_clean(native_bytes)
        )
    )
    reconciliation_entry: dict[str, Any] | None = None
    decision = independent_decision
    if needs_reconciliation:
        bound_files, bound_metadata = _bound_context_inputs(owner, context_path)
        reconciliation_prompt = _render_prompt("reconcile.md", prompt_values)
        emit("lane-transition", review_id=pack["review_id"], lane="reconciliation")
        _update_pipeline(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
            operation_id=pipeline_operation_id,
            stage="reconciliation",
        )
        reconciliation_entry, decision = _execute_decision_lane(
            owner,
            change_id=change_id,
            pack=pack,
            context_path=context_path,
            root_operation_id=pipeline_operation_id,
            lane_key="reconciliation",
            lane_kind="reconciliation",
            effort="high",
            prompt_text=reconciliation_prompt,
            input_only_workspace=True,
            stream_callback=(lambda event: emit(event.pop("event"), **event)) if stream_callback else None,
            extra_files={
                "context.json": safe_resolve(owner, context_path, must_exist=True),
                "native.txt": native_bytes,
                "semantic-independent.json": json.dumps(
                    independent_decision,
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8"),
                **{
                    f"specialists/{name}": payload
                    for name, payload in specialist_payloads.items()
                },
                **bound_files,
            },
            attempt_metadata={
                "workspace_mode": "input-only",
                **bound_metadata,
            },
        )
        if decision is None:
            return incomplete_result(owner, pack["review_id"])
    final_full_entry: dict[str, Any] | None = None
    if (
        pack["review_mode"] == "remediation"
        and pack["control_level"] != "routine"
        and not _has_actionable_review_finding(decision)
    ):
        final_budget = _final_full_budget(owner, pack["control_level"])
        final_prompt = _render_prompt(
            "final-full.md",
            {
                **prompt_values,
                "FINAL_FULL_COMMAND_TARGET": str(
                    min(FINAL_FULL_COMMAND_TARGET, final_budget.command_events)
                ),
                "FINAL_FULL_COMMAND_CEILING": str(
                    final_budget.command_events
                ),
            },
        )
        current_state = StateStore(owner).load(change_id)
        aggregate_before_final = sum(
            value
            for item in current_state.get("reviews", [])
            if isinstance(item, dict)
            and item.get("review_id") == pack["review_id"]
            and item.get("lane_key") != "semantic:final-full"
            for value in [processed_tokens(item.get("usage"))]
            if value is not None
        )
        try:
            final_inputs, final_metadata = _final_full_inputs(
                owner,
                pack=pack,
                context_path=context_path,
                native_bytes=native_bytes,
                independent_decision=independent_decision,
                targeted_decision=decision,
                specialist_payloads=specialist_payloads,
                aggregate_before=aggregate_before_final,
                budget=final_budget,
                prompt_text=final_prompt,
                schema_path=SCHEMAS_ROOT / "review-decision.schema.json",
            )
        except ReviewBudgetPlanningError as exc:
            _update_pipeline(
                owner,
                change_id=change_id,
                review_id=pack["review_id"],
                operation_id=pipeline_operation_id,
                stage="semantic:final-full",
                status="failed",
                failure_reason=str(exc),
                failure_kind="review-context",
            )
            return incomplete_result(owner, pack["review_id"])
        emit("lane-transition", review_id=pack["review_id"], lane="semantic:final-full")
        _update_pipeline(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
            operation_id=pipeline_operation_id,
            stage="semantic:final-full",
        )
        final_full_entry, final_decision = _execute_decision_lane(
            owner,
            change_id=change_id,
            pack=pack,
            context_path=context_path,
            root_operation_id=pipeline_operation_id,
            lane_key="semantic:final-full",
            lane_kind="semantic",
            effort=semantic_effort,
            prompt_text=final_prompt,
            extra_files=final_inputs,
            input_only_workspace=True,
            attempt_metadata=final_metadata,
            budget=final_budget,
            stream_callback=(lambda event: emit(event.pop("event"), **event)) if stream_callback else None,
        )
        if final_decision is None:
            return incomplete_result(owner, pack["review_id"])
        decision = final_decision
    finalization_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"dls:{change_id}:{pack['review_id']}:finalization",
        )
    )
    _, finalization, claimed_finalization = StateStore(
        owner
    ).claim_review_finalization(
        change_id,
        review_id=pack["review_id"],
        finalization_id=finalization_id,
        runner_pid=os.getpid(),
    )
    if not claimed_finalization:
        current = review_status(
            owner,
            change_id=change_id,
            review_id=pack["review_id"],
        )
        if current.get("status") == "completed" and current.get(
            "review_result_path"
        ):
            current.update(
                {
                    "dry_run": False,
                    "operation_id": effective_operation_id,
                    "review_pack_path": relative_pack_path,
                    "pack_created": False,
                    "delivery_receipt": delivery_receipt(
                        owner,
                        change_id=change_id,
                    ),
                }
            )
            return annotate_handoff(current)
        return incomplete_result(owner, pack["review_id"])
    try:
        report, imported, presentation = _finalize_review(
            owner,
            change_id=change_id,
            pack=pack,
            start_result=started,
            decision=decision,
            independent_entry=independent_entry,
            reconciliation_entry=reconciliation_entry,
            specialist_entries=specialist_entries,
            final_full_entry=final_full_entry,
            pipeline_operation_id=pipeline_operation_id,
        )
    except Exception as exc:
        StateStore(owner).finish_review_finalization(
            change_id,
            review_id=pack["review_id"],
            finalization_id=finalization_id,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    StateStore(owner).finish_review_finalization(
        change_id,
        review_id=pack["review_id"],
        finalization_id=finalization_id,
        status="completed",
    )
    result = {
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
        "runner_contract": pack.get("runner_contract", REVIEW_RUNNER_CONTRACT),
        "verdict": imported["verdict"],
        "finding_counts": imported["finding_counts"],
        "review_result_path": imported["review_result_path"],
        "remediation_manifest_path": imported.get(
            "remediation_manifest_path"
        ),
        "presentation": presentation,
        "next_action": imported["next_action"],
    }
    result["delivery_receipt"] = imported["delivery_receipt"]
    try:
        from .telemetry import cache_prune

        cache_prune(owner, change_id=change_id, apply=True)
    except Exception as exc:  # cleanup must never invalidate an imported review
        result["cleanup_warning"] = str(exc)
    emit(
        "delivery-receipt",
        review_id=pack["review_id"],
        delivery_receipt=result["delivery_receipt"],
    )
    emit(
        "completed",
        review_id=pack["review_id"],
        verdict=imported["verdict"],
        review_result_path=imported["review_result_path"],
        next_action=imported["next_action"]["id"],
    )
    return annotate_handoff(result)
