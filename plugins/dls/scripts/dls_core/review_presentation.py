"""Derived Codex presentation for canonical DLS review findings."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .repo import git_head


PRESENTATION_CONTRACT = "codex-inline-comments/v1"
ACTIONABLE_SEVERITIES = {"blocker", "should-fix"}
PRIORITY_BY_SEVERITY = {
    "blocker": 1,
    "should-fix": 2,
}
MAX_INLINE_SPAN = 8
_LOCATION_WITH_RANGES = re.compile(
    r"^(?P<path>.+?):(?P<ranges>"
    r"[1-9][0-9]*(?:-[1-9][0-9]*)?"
    r"(?:,[1-9][0-9]*(?:-[1-9][0-9]*)?)*"
    r")$"
)


def _one_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _owner_file(root: Path, raw_path: str) -> tuple[Path, str] | None:
    root_resolved = root.resolve()
    candidate = Path(raw_path)
    try:
        resolved = (
            candidate.resolve(strict=True)
            if candidate.is_absolute()
            else (root_resolved / candidate).resolve(strict=True)
        )
        relative = resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    if not resolved.is_file():
        return None
    return resolved, relative.as_posix()


def _line_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)


def _parse_location(root: Path, value: str) -> list[dict[str, Any]]:
    locations: list[dict[str, Any]] = []
    for fragment in value.split(";"):
        fragment = fragment.strip()
        if not fragment:
            continue
        match = _LOCATION_WITH_RANGES.match(fragment)
        raw_path = match.group("path") if match else fragment
        owned = _owner_file(root, raw_path)
        if owned is None:
            continue
        absolute, relative = owned
        try:
            count = _line_count(absolute)
        except OSError:
            continue
        if count < 1:
            continue
        ranges = match.group("ranges").split(",") if match else ["1"]
        for line_range in ranges:
            start_text, separator, end_text = line_range.partition("-")
            start = int(start_text)
            end = int(end_text) if separator else start
            if start > count:
                continue
            end = min(max(start, end), count)
            locations.append(
                {
                    "file": str(absolute),
                    "repository_path": relative,
                    "start": start,
                    "end": end,
                }
            )
    return locations


def _directive(
    *,
    title: str,
    body: str,
    file: str,
    start: int,
    end: int,
    priority: int,
) -> str:
    attributes = (
        f"title={json.dumps(title, ensure_ascii=False)} "
        f"body={json.dumps(body, ensure_ascii=False)} "
        f"file={json.dumps(file, ensure_ascii=False)} "
        f"start={start} end={end} priority={priority}"
    )
    return f"::code-comment{{{attributes}}}"


def build_review_presentation(root: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Build non-canonical UI hints from a validated ReviewIR."""

    report_head = report.get("head_sha")
    current_head = git_head(root)
    exact_head = isinstance(report_head, str) and report_head == current_head
    comments: list[dict[str, Any]] = []
    unplaced: list[dict[str, str]] = []
    errors: list[str] = []

    findings = report.get("findings", [])
    if not isinstance(findings, list):
        findings = []
        errors.append("review-findings-are-not-an-array")

    for finding in findings:
        if not isinstance(finding, dict):
            errors.append("review-finding-is-not-an-object")
            continue
        severity = finding.get("severity")
        if severity not in ACTIONABLE_SEVERITIES:
            continue
        finding_id = finding.get("id") or finding.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id:
            errors.append("actionable-finding-has-no-id")
            continue
        location_text = _one_line(finding.get("location"))
        if not exact_head:
            unplaced.append(
                {
                    "finding_id": finding_id,
                    "location": location_text,
                    "reason": "review-head-is-not-current",
                }
            )
            continue
        locations = _parse_location(root, location_text)
        if not locations:
            unplaced.append(
                {
                    "finding_id": finding_id,
                    "location": location_text,
                    "reason": "no-safe-owner-location",
                }
            )
            continue
        primary = locations[0]
        display_end = (
            primary["end"]
            if primary["end"] - primary["start"] + 1 <= MAX_INLINE_SPAN
            else primary["start"]
        )
        priority = PRIORITY_BY_SEVERITY[severity]
        title = f"[P{priority}] {finding_id} — {severity}"
        body_parts = [
            _one_line(finding.get("issue")),
            f"Impact: {_one_line(finding.get('impact'))}",
            f"Required fix: {_one_line(finding.get('required_fix'))}",
        ]
        body = " ".join(part for part in body_parts if part and not part.endswith(": "))
        comment = {
            "finding_id": finding_id,
            "severity": severity,
            "priority": priority,
            "title": title,
            "body": body,
            **primary,
            "source_end": primary["end"],
            "end": display_end,
            "related_locations": locations[1:],
        }
        comment["directive"] = _directive(
            title=title,
            body=body,
            file=primary["file"],
            start=primary["start"],
            end=display_end,
            priority=priority,
        )
        comments.append(comment)

    return {
        "contract": PRESENTATION_CONTRACT,
        "review_id": report.get("review_id"),
        "head_sha": report_head,
        "current_head": current_head,
        "exact_head": exact_head,
        "renderable": exact_head and bool(comments),
        "all_actionable_placed": not unplaced and not errors,
        "comments": comments,
        "unplaced_findings": unplaced,
        "errors": errors,
    }
