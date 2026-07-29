"""Deterministic, read-only Delivery Receipt projection."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from .errors import IntegrityError
from .io import (
    canonical_file_digest,
    canonical_text,
    read_json,
    redact_text,
    safe_resolve,
    sha256_bytes,
    sha256_file,
)
from .operations import (
    _finding_blocks,
    _latest_review_result,
    _read_review_result,
)
from .repo import (
    command_contract_digest,
    git_head,
    git_source_dirty_paths,
    git_source_snapshot_digest,
    is_git_repo,
    load_config,
)
from .state import StateStore, current_definition_digest, derived_approval_statuses

RECEIPT_CONTRACT = "dls-delivery-receipt/v1"
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_JSON_MAX_BYTES = 16 * 1024
RECEIPT_MARKDOWN_MAX_BYTES = 4 * 1024
RECEIPT_ITEM_LIMIT = 32

_H1_PATTERN = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_OUTCOME_HEADING_PATTERN = re.compile(
    r"^##\s+(?:Outcome|Product outcome|Результат|Итог)\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)
_OUTCOME_BOLD_PATTERN = re.compile(
    r"^\*\*(?:Outcome|Product outcome|Результат|Итог)[:.]?\*\*\s*(?P<body>.+)$",
    re.MULTILINE | re.IGNORECASE,
)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_:])/(?:Users|home|tmp|var|private|Volumes|workspace|workspaces|opt|etc)/\S+"
)
_LOCAL_PATH_ALIAS_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:\$HOME|\$CODEX_HOME|~)/(?:\S+)"
)

_STATUS_LABELS = {
    "accepted": "принято",
    "approved": "definition подтверждён",
    "blocked": "заблокировано",
    "candidate-ready": "кандидат готов к review",
    "definition-stale": "definition устарел",
    "draft": "черновик",
    "not-clear": "review не пройден",
    "pending": "ожидается",
    "review-clear": "review пройден",
    "source-dirty": "есть незакоммиченные изменения",
    "stale": "устарело",
}


def _digest(value: dict[str, Any]) -> str:
    return sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _bounded(items: list[Any], *, limit: int = RECEIPT_ITEM_LIMIT) -> dict[str, Any]:
    effective_limit = min(RECEIPT_ITEM_LIMIT, max(0, limit))
    return {
        "items": items[:effective_limit],
        "omitted_count": max(0, len(items) - effective_limit),
    }


def _safe_text(value: object, *, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    normalized = " ".join(value.split())
    normalized = _ABSOLUTE_PATH_PATTERN.sub("[PATH]", normalized)
    normalized = _LOCAL_PATH_ALIAS_PATTERN.sub("[PATH]", normalized)
    normalized = redact_text(normalized)
    encoded = normalized.encode("utf-8")
    if len(encoded) <= limit:
        return normalized
    clipped = encoded[: max(0, limit - 3)]
    while True:
        try:
            return clipped.decode("utf-8").rstrip() + "..."
        except UnicodeDecodeError:
            clipped = clipped[:-1]


def _artifact_text(root: Path, metadata: object) -> str | None:
    if not isinstance(metadata, dict):
        return None
    relative = metadata.get("path")
    if not isinstance(relative, str):
        return None
    path = safe_resolve(root, relative, must_exist=True)
    try:
        return canonical_text(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return None


def _title_and_outcome(root: Path, state: dict[str, Any]) -> tuple[str, str | None]:
    artifacts = state.get("artifacts", {})
    selected_texts: list[str] = []
    for key in ("change", "epic", "spec"):
        text = _artifact_text(root, artifacts.get(key))
        if text is not None:
            selected_texts.append(text)
    title = ""
    for text in selected_texts:
        match = _H1_PATTERN.search(text)
        if match:
            title = _safe_text(match.group(1), limit=240)
            if title:
                break
    if not title:
        slug = _safe_text(state.get("slug"), limit=160)
        title = state["change_id"] + (f" — {slug}" if slug else "")
    outcome: str | None = None
    for text in selected_texts:
        match = _OUTCOME_HEADING_PATTERN.search(text) or _OUTCOME_BOLD_PATTERN.search(text)
        if match:
            value = _safe_text(match.group("body"), limit=320)
            if value:
                outcome = value
                break
    return title, outcome


def _approval_projection(
    approvals: list[dict[str, Any]],
    *,
    decision: str,
) -> tuple[str, dict[str, Any] | None]:
    matching = [item for item in approvals if item.get("decision") == decision]
    current = next(
        (item for item in reversed(matching) if item.get("status") == "current"),
        None,
    )
    if current is not None:
        return "approved", current
    stale = next(
        (item for item in reversed(matching) if item.get("status") == "stale"),
        None,
    )
    return ("stale", stale) if stale is not None else ("pending", None)


def _finding_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"blocker": 0, "should-fix": 0, "note": 0}
    for finding in findings:
        severity = finding.get("severity")
        if severity in counts:
            counts[severity] += 1
    return counts


def _finding_projection(finding: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": finding.get("id"),
        "severity": finding.get("severity"),
        "kind": finding.get("kind"),
        "blocks": sorted(_finding_blocks(finding)),
        "ticket_ids": _bounded(
            sorted(item for item in finding.get("ticket_ids", []) if isinstance(item, str)),
            limit=6,
        ),
        "requirement_ids": _bounded(
            sorted(
                item for item in finding.get("requirement_ids", []) if isinstance(item, str)
            ),
            limit=6,
        ),
    }
    location = finding.get("location")
    if isinstance(location, str):
        repository_path = location.split(":", 1)[0].strip()
        if (
            repository_path
            and not Path(repository_path).is_absolute()
            and ".." not in Path(repository_path).parts
        ):
            item["location"] = _safe_text(location, limit=256)
    if isinstance(location, dict):
        path = location.get("path")
        if isinstance(path, str) and path and not Path(path).is_absolute():
            projected_location: dict[str, Any] = {"path": path}
            for key in ("line", "end_line"):
                if isinstance(location.get(key), int):
                    projected_location[key] = location[key]
            item["location"] = projected_location
    return item


def _review_projection(
    root: Path,
    state: dict[str, Any],
    *,
    head_sha: str | None,
    source_clean: bool,
    accepted_head_sha: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    entries = [
        item
        for item in state.get("reviews", [])
        if isinstance(item, dict) and item.get("kind") == "result"
    ]
    latest = _latest_review_result(state)
    if latest is None:
        return (
            {
                "status": "pending",
                "current": False,
                "review_id": None,
                "reviewed_head_sha": None,
                "result_path": None,
                "latest_finding_counts": _finding_counts([]),
                "current_finding_counts": _finding_counts([]),
                "findings": _bounded([]),
                "historical": {
                    "review_count": 0,
                    "finding_counts": _finding_counts([]),
                },
            },
            None,
            False,
        )
    result_path, report = _read_review_result(root, latest)
    findings = [item for item in report.get("findings", []) if isinstance(item, dict)]
    current = bool(
        head_sha
        and report.get("head_sha") in {head_sha, accepted_head_sha}
        and source_clean
    )
    historical_counts = Counter({"blocker": 0, "should-fix": 0, "note": 0})
    for entry in entries[:-1]:
        _, historical_report = _read_review_result(root, entry)
        historical_counts.update(_finding_counts(
            [item for item in historical_report.get("findings", []) if isinstance(item, dict)]
        ))
    projection = {
        "status": report.get("verdict") if current else "stale",
        "current": current,
        "review_id": report.get("review_id") or latest.get("review_id"),
        "reviewed_head_sha": report.get("head_sha") or latest.get("head_sha"),
        "result_path": result_path,
        "result_digest": latest.get("result_digest"),
        "latest_finding_counts": _finding_counts(findings),
        "current_finding_counts": _finding_counts(findings) if current else _finding_counts([]),
        "findings": _bounded(
            [_finding_projection(item) for item in findings],
            limit=8,
        ),
        "historical": {
            "review_count": max(0, len(entries) - 1),
            "finding_counts": dict(historical_counts),
        },
    }
    return projection, report, current


def _validation_projection(
    root: Path,
    state: dict[str, Any],
    *,
    proof_head_sha: str | None = None,
) -> dict[str, Any]:
    config = load_config(root)
    required = list(config.get("policy", {}).get("review_required_commands", []))
    current_head = git_head(root)
    validation_head = proof_head_sha or current_head
    current_source_digest = git_source_snapshot_digest(root)
    latest_pass: dict[str, tuple[str, dict[str, Any]]] = {}
    for relative in reversed(state.get("evidence", [])):
        if not isinstance(relative, str):
            continue
        record = read_json(safe_resolve(root, relative, must_exist=True))
        command_id = record.get("command_id")
        if not isinstance(command_id, str) or command_id in latest_pass:
            continue
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        if (
            command_id in required
            and record.get("git_sha") == validation_head
            and (
                validation_head != current_head
                or record.get("source_digest") == current_source_digest
            )
            and record.get("exit_code") == 0
            and not extra.get("timed_out", False)
            and not extra.get("output_overflow", False)
            and extra.get("command_contract_digest")
            == command_contract_digest(root, command_id)
        ):
            latest_pass[command_id] = (relative, record)
    passed: list[dict[str, Any]] = []
    for command_id in required:
        evidence = latest_pass.get(command_id)
        if evidence is None:
            continue
        relative, record = evidence
        extra = record.get("extra") if isinstance(record.get("extra"), dict) else {}
        path = safe_resolve(root, relative, must_exist=True)
        passed.append(
            {
                "command_id": command_id,
                "evidence_id": record.get("id"),
                "evidence_path": relative,
                "evidence_digest": sha256_file(path),
                "command_contract_digest": extra.get("command_contract_digest"),
                "git_sha": record.get("git_sha"),
                "source_digest": record.get("source_digest"),
            }
        )
    passed_ids = {item["command_id"] for item in passed}
    missing = [command_id for command_id in required if command_id not in passed_ids]
    status = "not-configured" if not required else ("passing" if not missing else "incomplete")
    return {
        "status": status,
        "required_count": len(required),
        "passing_count": len(passed),
        "required_commands": _bounded(required),
        "passing_evidence": _bounded(passed, limit=16),
        "missing_commands": _bounded(missing),
    }


def _stage_projection(
    report: dict[str, Any] | None,
    *,
    current: bool,
    stage: str,
) -> dict[str, Any]:
    blockers: list[str] = []
    if current and report is not None:
        for finding in report.get("findings", []):
            if not isinstance(finding, dict):
                continue
            if (
                finding.get("severity") in {"blocker", "should-fix"}
                and stage in _finding_blocks(finding)
                and isinstance(finding.get("id"), str)
            ):
                blockers.append(finding["id"])
    return {
        "status": "blocked" if blockers else "not-evaluated",
        "blocking_finding_ids": _bounded(sorted(blockers)),
    }


def _render_markdown(receipt: dict[str, Any]) -> str:
    definition = receipt["definition"]["status"]
    implementation = receipt["implementation"]
    validation = receipt["validation"]
    review = receipt["review"]
    acceptance = receipt["acceptance"]["status"]
    next_action = receipt["next_action"]
    lines = [
        f"# Delivery Receipt — {receipt['change_id']}",
        "",
        f"**{receipt['title']}**",
    ]
    if receipt.get("outcome"):
        lines.extend(["", receipt["outcome"]])
    head = receipt.get("head_sha")
    lines.extend(
        [
            "",
            "## Текущее состояние",
            "",
            f"- Lifecycle: **{_STATUS_LABELS.get(receipt['lifecycle'], receipt['lifecycle'])}**",
            f"- Revision: `{head[:12] if isinstance(head, str) else 'нет Git revision'}`",
            f"- Исходники: **{'чистые' if receipt['source_clean'] else 'есть незакоммиченные изменения'}**",
            f"- Контроль: `{receipt['control_level']}`",
            "",
            "## Доказательства",
            "",
            f"- Definition: **{_STATUS_LABELS.get(definition, definition)}**",
            (
                "- Implementation: "
                f"candidate `{implementation['candidate_status']}`, "
                f"tickets {implementation['implemented_ticket_count']}/{implementation['ticket_count']} implemented"
            ),
            (
                "- Validation: "
                f"**{validation['status']}**, "
                f"{validation['passing_count']}/{validation['required_count']} required checks"
            ),
            (
                "- Review: "
                f"**{_STATUS_LABELS.get(review['status'], review['status'])}**, "
                f"findings {review['latest_finding_counts']['blocker']} blocker / "
                f"{review['latest_finding_counts']['should-fix']} should-fix / "
                f"{review['latest_finding_counts']['note']} note"
            ),
            f"- Acceptance: **{_STATUS_LABELS.get(acceptance, acceptance)}**",
            f"- Release: **{receipt['release']['status']}**",
            f"- Production: **{receipt['production']['status']}**",
            "",
            "## Следующий шаг",
            "",
            f"**{next_action.get('id', 'none')}**"
            + (f" — {next_action['detail']}" if next_action.get("detail") else ""),
            "",
            "> Review, acceptance, release и production — отдельные границы. Один статус не подменяет другой.",
            "",
        ]
    )
    dependencies = receipt.get("dependencies", {})
    parallelism = receipt.get("parallelism", {})
    if dependencies.get("count", 0) or parallelism.get("exact_overlap_count", 0):
        lines[-1:-1] = [
            "## Координация",
            "",
            (
                "- Dependencies: "
                f"**{dependencies.get('satisfied_count', 0)}/"
                f"{dependencies.get('count', 0)} satisfied**, "
                f"{dependencies.get('blocked_count', 0)} blocking this stage"
            ),
            (
                "- Overlap: "
                f"{parallelism.get('exact_overlap_count', 0)} exact files / "
                f"{parallelism.get('proximity_count', 0)} nearby path groups"
            ),
            "",
        ]
    markdown = "\n".join(lines)
    if len(markdown.encode("utf-8")) > RECEIPT_MARKDOWN_MAX_BYTES:
        raise IntegrityError("Delivery Receipt Markdown exceeds 4 KiB")
    return markdown


def delivery_receipt(root: Path, *, change_id: str) -> dict[str, Any]:
    """Build a byte-stable derived receipt without writing state or artifacts."""
    from .candidate_runner import candidate_status
    from .review_runner import review_status
    from .telemetry import _owner_root, resolve_delivery_next_action

    owner, _ = _owner_root(root, change_id)
    state = StateStore(owner).load(change_id)
    head_sha = git_head(owner)
    dirty_paths = git_source_dirty_paths(owner) if is_git_repo(owner) else []
    source_clean = not dirty_paths
    source_digest = git_source_snapshot_digest(owner) if is_git_repo(owner) else None
    definition_digest = current_definition_digest(owner, state)
    approvals = derived_approval_statuses(owner, state)
    definition_status, definition_approval = _approval_projection(
        approvals, decision="definition"
    )
    acceptance_status, acceptance_approval = _approval_projection(
        approvals, decision="accept"
    )
    if acceptance_status == "approved" and not source_clean:
        acceptance_status = "stale"
    title, outcome = _title_and_outcome(owner, state)
    candidate = candidate_status(
        owner,
        change_id=change_id,
        _inspect_task_context=False,
    )
    review_readiness = (
        review_status(
            owner,
            change_id=change_id,
            _inspect_task_context=False,
        )
        if source_clean
        else {
            "status": "not-prepared",
            "next_action": {
                "id": "commit-product-source",
                "detail": "product source is dirty",
            },
        }
    )

    ticket_items = [
        {"id": ticket_id, "status": payload.get("status")}
        for ticket_id, payload in sorted(state.get("tickets", {}).items())
        if isinstance(payload, dict)
    ]
    ticket_counts = Counter(
        item["status"] for item in ticket_items if isinstance(item.get("status"), str)
    )
    implemented_count = sum(
        count
        for status, count in ticket_counts.items()
        if status in {"implemented", "validated", "done"}
    )
    accepted_current = bool(
        acceptance_status == "approved"
        and acceptance_approval is not None
        and source_clean
    )
    accepted_head_sha = (
        acceptance_approval.get("git_sha")
        if accepted_current and acceptance_approval is not None
        else None
    )
    validation = _validation_projection(
        owner,
        state,
        proof_head_sha=accepted_head_sha,
    )
    review, latest_report, review_current = _review_projection(
        owner,
        state,
        head_sha=head_sha,
        source_clean=source_clean,
        accepted_head_sha=accepted_head_sha,
    )
    if accepted_current:
        dependency_stage = "acceptance"
    elif review_current and review.get("status") == "review-clear":
        dependency_stage = "acceptance"
    elif candidate.get("prepared") or review_current:
        dependency_stage = "review"
    elif definition_status == "approved":
        dependency_stage = "implementation"
    else:
        dependency_stage = "definition"
    from .parallel_delivery import change_readiness

    delivery_readiness = change_readiness(
        owner,
        change_id=change_id,
        stage=dependency_stage,
        include_overlap=dependency_stage in {"review", "acceptance"},
    )
    if accepted_current:
        lifecycle = "accepted"
        acceptance_public_status = "accepted"
    else:
        acceptance_public_status = acceptance_status
        if not source_clean:
            lifecycle = "source-dirty"
        elif review_current and review.get("status") in {
            "review-clear",
            "not-clear",
            "blocked",
        }:
            lifecycle = review["status"]
        elif candidate.get("prepared") and candidate.get("exact_head"):
            lifecycle = "candidate-ready"
        elif definition_status == "approved":
            lifecycle = "approved"
        elif definition_status == "stale":
            lifecycle = "definition-stale"
        else:
            lifecycle = "draft"

    artifact_items = [
        {
            "key": key,
            "path": metadata.get("path"),
            "digest": canonical_file_digest(
                safe_resolve(owner, metadata["path"], must_exist=True)
            ),
        }
        for key, metadata in sorted(state.get("artifacts", {}).items())
        if isinstance(metadata, dict) and isinstance(metadata.get("path"), str)
    ]
    next_action = (
        {
            "id": "delivery-complete",
            "detail": "DLS core принят; release и production остаются отдельными границами",
        }
        if accepted_current
        else (
            delivery_readiness["next_action"]
            if delivery_readiness["next_action"] is not None
            else (
                review_readiness["next_action"]
                if not source_clean
                else resolve_delivery_next_action(candidate, review_readiness)
            )
        )
    )
    safe_next_action = {
        "id": next_action.get("id"),
        "detail": _safe_text(next_action.get("detail"), limit=320) or None,
    }
    projection: dict[str, Any] = {
        "ok": True,
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "contract": RECEIPT_CONTRACT,
        "change_id": change_id,
        "title": title,
        "outcome": outcome,
        "state_revision": state["state_revision"],
        "head_sha": head_sha,
        "source_digest": source_digest,
        "source_clean": source_clean,
        "control_level": state["control_level"],
        "lifecycle": lifecycle,
        "definition": {
            "status": definition_status,
            "digest": definition_digest,
            "approval_id": (
                definition_approval.get("id") if definition_approval is not None else None
            ),
            "artifacts": _bounded(artifact_items, limit=16),
        },
        "implementation": {
            "candidate_status": candidate.get("status"),
            "candidate_head_sha": candidate.get("candidate_head"),
            "exact_head": bool(candidate.get("exact_head")),
            "prepared": bool(candidate.get("prepared")),
            "ticket_count": len(ticket_items),
            "implemented_ticket_count": implemented_count,
            "ticket_counts": dict(sorted(ticket_counts.items())),
            "tickets": _bounded(ticket_items),
        },
        "validation": validation,
        "review": review,
        "acceptance": {
            "status": acceptance_public_status,
            "approval_id": (
                acceptance_approval.get("id") if acceptance_approval is not None else None
            ),
            "head_sha": (
                acceptance_approval.get("git_sha") if acceptance_approval is not None else None
            ),
        },
        "release": _stage_projection(latest_report, current=review_current, stage="release"),
        "production": _stage_projection(
            latest_report, current=review_current, stage="production"
        ),
        "dependencies": {
            "stage": dependency_stage,
            "contract_digest": delivery_readiness["dependencies"]["digest"],
            "count": len(delivery_readiness["dependencies"]["items"]),
            "satisfied_count": sum(
                1
                for item in delivery_readiness["dependencies"]["items"]
                if item["satisfied"]
            ),
            "blocked_count": delivery_readiness["dependencies"]["blocked_count"],
            "items": _bounded(
                [
                    {
                        "change_id": item["change_id"],
                        "blocks_stage": item["blocks_stage"],
                        "requires": item["requires"],
                        "applies": item["applies"],
                        "satisfied": item["satisfied"],
                        "reason": item["reason"],
                    }
                    for item in delivery_readiness["dependencies"]["items"]
                ]
            ),
        },
        "parallelism": {
            "contract_digest": delivery_readiness["overlap"]["digest"],
            "status": delivery_readiness["overlap"]["status"],
            "blocked": delivery_readiness["overlap"]["blocked"],
            "exact_overlap_count": delivery_readiness["overlap"][
                "exact_overlap_count"
            ],
            "proximity_count": delivery_readiness["overlap"]["proximity_count"],
        },
        "next_action": safe_next_action,
    }
    receipt_digest = _digest(projection)
    markdown = _render_markdown(projection)
    receipt = {
        **projection,
        "receipt_digest": receipt_digest,
        "markdown_digest": sha256_bytes(markdown.encode("utf-8")),
        "markdown": markdown,
    }
    encoded = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    if len(encoded) > RECEIPT_JSON_MAX_BYTES - 256:
        raise IntegrityError("Delivery Receipt JSON exceeds 16 KiB")
    return receipt
