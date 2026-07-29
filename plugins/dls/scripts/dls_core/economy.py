"""Risk-adaptive review budgets shared by native and semantic runners."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .repo import load_config


@dataclass(frozen=True)
class ReviewBudget:
    aggregate_tokens: int
    lane_tokens: int
    command_events: int
    timeout_seconds: int
    transcript_bytes: int
    aggregate_recovery_tokens: int | None = None
    lane_recovery_tokens: int | None = None

    @property
    def aggregate_ceiling(self) -> int:
        return self.aggregate_recovery_tokens or self.aggregate_tokens

    @property
    def lane_ceiling(self) -> int:
        return self.lane_recovery_tokens or self.lane_tokens


DEFAULT_REVIEW_BUDGETS = {
    "routine": ReviewBudget(
        750_000, 750_000, 12, 600, 384 * 1024, 825_000, 825_000
    ),
    "standard": ReviewBudget(
        3_000_000, 1_500_000, 24, 900, 768 * 1024, 3_300_000, 1_650_000
    ),
    "critical": ReviewBudget(
        8_000_000, 6_000_000, 48, 1_200, 1536 * 1024, 8_800_000, 6_600_000
    ),
}

RECOVERY_ABSOLUTE_LIMITS = {
    "routine": {"aggregate": 75_000, "lane": 75_000},
    "standard": {"aggregate": 300_000, "lane": 150_000},
    "critical": {"aggregate": 800_000, "lane": 600_000},
}


def _recovery_ceiling(target: int, *, absolute_limit: int) -> int:
    return target + min(target // 10, absolute_limit)


def review_budget(root: Path, control_level: str) -> ReviewBudget:
    if control_level not in DEFAULT_REVIEW_BUDGETS:
        raise ConfigError(f"Unsupported review budget level: {control_level}")
    default = DEFAULT_REVIEW_BUDGETS[control_level]
    configured = load_config(root).get("review_budgets", {}).get(control_level, {})
    if not isinstance(configured, dict):
        raise ConfigError(f"review_budgets.{control_level} must be a table")
    values: dict[str, Any] = {
        "aggregate_tokens": default.aggregate_tokens,
        "lane_tokens": default.lane_tokens,
        "command_events": default.command_events,
        "timeout_seconds": default.timeout_seconds,
        "transcript_bytes": default.transcript_bytes,
    }
    for key in values:
        if key not in configured:
            continue
        value = configured[key]
        if not isinstance(value, int) or value <= 0:
            raise ConfigError(
                f"review_budgets.{control_level}.{key} must be a positive integer"
            )
        values[key] = value
    limits = RECOVERY_ABSOLUTE_LIMITS[control_level]
    values["aggregate_recovery_tokens"] = _recovery_ceiling(
        values["aggregate_tokens"], absolute_limit=limits["aggregate"]
    )
    values["lane_recovery_tokens"] = _recovery_ceiling(
        values["lane_tokens"], absolute_limit=limits["lane"]
    )
    return ReviewBudget(**values)


def processed_tokens(usage: object) -> int | None:
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    if input_tokens < 0 or output_tokens < 0:
        return None
    if input_tokens == 0 and output_tokens == 0:
        return None
    return input_tokens + output_tokens


def token_budget_failure(
    usage: object,
    *,
    aggregate_before: int,
    budget: ReviewBudget,
) -> str | None:
    lane_tokens = processed_tokens(usage)
    if lane_tokens is None:
        return None
    if lane_tokens > budget.lane_ceiling:
        return (
            f"lane processed_tokens={lane_tokens} exceeds "
            f"budget={budget.lane_ceiling}"
        )
    aggregate = aggregate_before + lane_tokens
    if aggregate > budget.aggregate_ceiling:
        return (
            f"aggregate processed_tokens={aggregate} exceeds "
            f"budget={budget.aggregate_ceiling}"
        )
    return None


def token_budget_warning(
    usage: object,
    *,
    aggregate_before: int,
    budget: ReviewBudget,
) -> dict[str, int] | None:
    """Describe a completed call above target but inside its recovery ceiling."""
    lane_tokens = processed_tokens(usage)
    if lane_tokens is None:
        return None
    aggregate = aggregate_before + lane_tokens
    if lane_tokens <= budget.lane_tokens and aggregate <= budget.aggregate_tokens:
        return None
    if lane_tokens > budget.lane_ceiling or aggregate > budget.aggregate_ceiling:
        return None
    return {
        "lane_processed_tokens": lane_tokens,
        "lane_target_tokens": budget.lane_tokens,
        "lane_ceiling_tokens": budget.lane_ceiling,
        "lane_overrun_tokens": max(0, lane_tokens - budget.lane_tokens),
        "aggregate_processed_tokens": aggregate,
        "aggregate_target_tokens": budget.aggregate_tokens,
        "aggregate_ceiling_tokens": budget.aggregate_ceiling,
        "aggregate_overrun_tokens": max(0, aggregate - budget.aggregate_tokens),
    }
