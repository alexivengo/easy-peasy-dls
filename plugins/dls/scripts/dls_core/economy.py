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


DEFAULT_REVIEW_BUDGETS = {
    "routine": ReviewBudget(750_000, 750_000, 12, 600, 384 * 1024),
    "standard": ReviewBudget(3_000_000, 1_500_000, 24, 900, 768 * 1024),
    "critical": ReviewBudget(5_000_000, 2_500_000, 48, 1_200, 1536 * 1024),
}


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
    if lane_tokens > budget.lane_tokens:
        return (
            f"lane processed_tokens={lane_tokens} exceeds "
            f"budget={budget.lane_tokens}"
        )
    aggregate = aggregate_before + lane_tokens
    if aggregate > budget.aggregate_tokens:
        return (
            f"aggregate processed_tokens={aggregate} exceeds "
            f"budget={budget.aggregate_tokens}"
        )
    return None
