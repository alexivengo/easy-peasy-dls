"""DLS command operations."""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION, VERSION
from .errors import ConfigError, IntegrityError, UsageError
from .io import (
    atomic_write_json,
    atomic_write_text,
    canonical_file_digest,
    read_json,
    redact_text,
    safe_resolve,
    sha256_bytes,
    sha256_file,
    utc_now,
    write_immutable_json,
)
from .repo import (
    PLUGIN_ROOT,
    PROFILES_ROOT,
    SCHEMAS_ROOT,
    TEMPLATES_ROOT,
    allowed_environment,
    command_config,
    copy_asset,
    git_changed_files,
    git_head,
    git_merge_base,
    git_source_dirty_paths,
    git_source_snapshot_digest,
    is_git_repo,
    load_config,
    render_template,
    run_git,
)
from .state import (
    CONTROL_LEVELS,
    IMPACT_TAGS,
    WORK_KINDS,
    StateStore,
    current_definition_digest,
    derived_approval_statuses,
    initial_state,
    validate_change_id,
)
from .worktrees import resolve_registered_worktree

AFFIRMATIVE_PATTERN = re.compile(
    r"^\s*(yes|y|approve|approved|confirm|confirmed|да|ок|фиксируем|"
    r"подтверждаю|принимаю)\b",
    re.IGNORECASE,
)
TICKET_ID_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*-)?T[0-9]{2,}$"
)
TICKET_HEADING_PATTERN = re.compile(
    r"^##\s+((?:[A-Za-z0-9][A-Za-z0-9._-]*-)?T[0-9]{2,})\b",
    re.MULTILINE,
)
ARTIFACT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
REQUIREMENT_PREFIX_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{0,15}$")
TICKET_STATUSES = {
    "planned",
    "ready",
    "in-progress",
    "implemented",
    "validated",
    "blocked",
    "done",
}
TICKET_TRANSITIONS = {
    None: {"planned", "ready", "in-progress", "blocked"},
    "planned": {"ready", "in-progress", "blocked"},
    "ready": {"in-progress", "blocked"},
    "in-progress": {"implemented", "blocked"},
    "implemented": {"validated", "in-progress", "blocked"},
    "validated": {"done", "in-progress", "blocked"},
    "blocked": {"planned", "ready", "in-progress"},
    "done": {"in-progress"},
}
FINDING_SEVERITIES = {"blocker", "should-fix", "note"}
FINDING_KINDS = {"defect", "validation-gap", "governance", "external", "design"}
REVIEW_VERDICTS = {"review-clear", "not-clear", "blocked"}
DISPOSITION_STATUSES = {
    "addressed",
    "verified",
    "waived",
    "reopened",
    "note",
    "resolved",
}
WRITABLE_DISPOSITION_STATUSES = {
    "addressed",
    "waived",
    "reopened",
    "note",
    "resolved",
}
REVIEW_BLOCK_STAGES = {"review", "acceptance", "release", "production"}
REVIEW_TICKET_VERDICTS = {"clear", "not-clear", "blocked"}
PRIOR_FINDING_VERDICTS = {"verified", "still-open", "regressed", "waived"}
REVIEW_PACK_SCHEMA_VERSION = 2
REVIEW_IR_SCHEMA_VERSION = 2
REVIEW_MODES = {"full", "remediation"}
NATIVE_REVIEW_MODEL = "gpt-5.6-terra"
NATIVE_REVIEW_REASONING_EFFORT = "high"
NATIVE_REVIEW_TIMEOUT_SECONDS = 1800
NATIVE_REVIEW_MAX_OUTPUT_BYTES = 262144
NATIVE_REVIEW_TRANSCRIPT_MAX_BYTES = 262144

RISK_LENS_DEFINITIONS = (
    {
        "id": "contract-trust",
        "tags": {
            "public-api",
            "compatibility",
            "external-dependency",
            "security-privacy",
            "auth",
        },
        "focus": "Protocol, compatibility, authentication, trust boundaries, and abuse cases.",
        "priority": 0,
    },
    {
        "id": "concurrency-reliability",
        "tags": {"concurrency", "availability", "performance-cost"},
        "focus": "Task ownership, cancellation, deadlines, races, retries, and failure recovery.",
        "priority": 1,
    },
    {
        "id": "data-migration",
        "tags": {"data-migration", "data-loss", "money", "irreversible"},
        "focus": "Data integrity, migration safety, rollback, idempotency, and irreversible harm.",
        "priority": 2,
    },
    {
        "id": "ux-interaction",
        "tags": {"user-interface"},
        "focus": "User flows, state coverage, accessibility, localization, and interaction failure.",
        "priority": 3,
    },
    {
        "id": "architecture-integration",
        "tags": {"architecture"},
        "focus": "Architectural boundaries, integration seams, ownership, and cross-ticket behavior.",
        "priority": 4,
    },
)


def init_repository(root: Path, *, dry_run: bool) -> dict[str, Any]:
    actions = [
        ".dls/config.toml",
        ".dls/.gitignore",
        ".dls/state/",
        ".dls/evidence/",
        ".dls/reviews/",
        ".dls/cache/",
    ]
    if dry_run:
        return {"ok": True, "dry_run": True, "actions": actions}
    dls_root = root / ".dls"
    copy_asset(TEMPLATES_ROOT / "config.toml", dls_root / "config.toml")
    copy_asset(TEMPLATES_ROOT / "dls.gitignore", dls_root / ".gitignore")
    for name in ("state", "evidence", "reviews", "cache"):
        (dls_root / name).mkdir(parents=True, exist_ok=True)
    return {
        "ok": True,
        "dry_run": False,
        "actions": actions,
        "git_repository": is_git_repo(root),
    }


def new_change(
    root: Path,
    *,
    change_id: str,
    slug: str,
    title: str,
    work_kind: str,
    control_level: str,
    impact_tags: list[str],
    roadmap_epic: bool,
    with_tickets: bool,
    with_adr: bool,
    outcome: str,
    operation_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    validate_change_id(change_id)
    if work_kind not in WORK_KINDS:
        raise UsageError(f"Invalid work kind: {work_kind}")
    if control_level not in CONTROL_LEVELS | {"micro"}:
        raise UsageError(f"Invalid control level: {control_level}")
    unknown_tags = sorted(set(impact_tags) - IMPACT_TAGS)
    if unknown_tags:
        raise UsageError(f"Unknown impact tags: {', '.join(unknown_tags)}")
    if control_level == "micro":
        if roadmap_epic or with_tickets or with_adr:
            raise UsageError("Micro work cannot create epic, ticket, or ADR artifacts")
        return {
            "ok": True,
            "dry_run": dry_run,
            "change_id": change_id,
            "path": "micro",
            "artifacts": [],
            "message": "Micro work requires no DLS artifact.",
        }
    if with_adr and control_level not in {"standard", "critical"}:
        raise UsageError("ADR is available only for standard or critical work")
    if roadmap_epic and control_level not in {"standard", "critical"}:
        raise UsageError("Roadmap epic requires standard or critical control")
    if with_tickets and (control_level == "routine" or work_kind in {"spike", "hotfix"}):
        raise UsageError("Tickets are not part of routine, spike, or hotfix packages")
    config = load_config(root)
    docs_root = safe_resolve(root, config["docs_root"])
    safe_slug = _slugify(slug)
    change_dir = docs_root / f"{change_id}-{safe_slug}"
    artifact_templates: list[tuple[str, str]]
    if work_kind in {"spike", "hotfix"} or control_level == "routine":
        artifact_templates = [("change", "CHANGE.md")]
    elif roadmap_epic or control_level == "critical":
        artifact_templates = [
            ("epic", "EPIC.md"),
            ("spec", "SPEC.md"),
            ("tickets", "TICKETS.md"),
        ]
    else:
        artifact_templates = [("spec", "SPEC.md")]
        if with_tickets:
            artifact_templates.append(("tickets", "TICKETS.md"))
    if with_adr:
        artifact_templates.append(("adr", "ADR.md"))
    relative_contract = f"{config['docs_root']}/{change_id}-{safe_slug}/SPEC.md"
    values = {
        "TITLE": title,
        "ID": change_id,
        "KIND": work_kind,
        "OUTCOME": outcome,
        "SCOPE_ITEM": "Deliver the stated outcome within the approved boundaries.",
        "NON_GOAL": "Unrelated cleanup or behavior changes.",
        "REQUIREMENT": "The stated outcome is demonstrably satisfied.",
        "APPROACH": "Select the smallest coherent approach after repository discovery.",
        "DISCOVERY": "Record only repository facts relevant to this change.",
        "INTERFACES": "Describe affected interfaces, state, and failure behavior.",
        "CROSS_CUTTING": "Record applicable concerns; mark the rest not applicable.",
        "VALIDATION": "Run the narrowest checks that prove the changed behavior.",
        "RISK_RATIONALE": "Confirm consequences, ambiguity, breadth, and reversibility.",
        "UI_SOURCE": (
            "Record an accepted source or explicit bypass."
            if "user-interface" in impact_tags
            else "Not applicable."
        ),
        "SUCCESS_MEASURE": "The accepted outcome is observable.",
        "DEPENDENCY": "None identified yet.",
        "CONTRACT_FILE": relative_contract,
        "TICKET_TITLE": "Implement the first coherent slice",
        "ADR_ID": "001",
        "CONTEXT": "Describe the durable decision context.",
        "DECISION": "Record the selected option after focused architecture review.",
        "ALTERNATIVES": "Record credible alternatives and why they were not selected.",
        "CONSEQUENCES": "Record benefits, costs, risks, and follow-up obligations.",
    }
    planned: list[dict[str, str]] = []
    for key, template_name in artifact_templates:
        rendered = render_template(template_name, values)
        relative_path = f"{config['docs_root']}/{change_id}-{safe_slug}/{template_name}"
        planned.append(
            {
                "key": key,
                "path": relative_path,
                "content": rendered,
            }
        )
    state_store = StateStore(root)
    if state_store.path(change_id).exists():
        existing = state_store.load(change_id)
        if operation_id and _has_operation(existing, operation_id):
            existing_operation = _operation(existing, operation_id)
            assert existing_operation is not None
            _require_operation_kind(existing_operation, "state-create")
            return {
                "ok": True,
                "dry_run": False,
                "changed": False,
                "change_id": change_id,
                "state_revision": existing["state_revision"],
                "operation_id": operation_id,
                "artifacts": [
                    metadata["path"]
                    for _, metadata in sorted(existing["artifacts"].items())
                ],
            }
        raise IntegrityError(f"Change already exists: {change_id}")
    if change_dir.exists():
        raise IntegrityError(f"Artifact directory already exists: {change_dir}")
    effective_operation_id = operation_id or str(uuid.uuid4())
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "operation_id": effective_operation_id,
            "artifacts": [item["path"] for item in planned],
        }
    artifacts: dict[str, dict[str, str]] = {}
    for item in planned:
        path = safe_resolve(root, item["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, item["content"], backup=False)
        artifacts[item["key"]] = {"path": item["path"]}
    state = initial_state(
        change_id=change_id,
        slug=safe_slug,
        work_kind=work_kind,
        control_level=control_level,
        impact_tags=impact_tags,
        artifacts=artifacts,
        operation_id=effective_operation_id,
    )
    state_store.create(state)
    return {
        "ok": True,
        "dry_run": False,
        "changed": True,
        "change_id": change_id,
        "state_revision": 1,
        "operation_id": effective_operation_id,
        "artifacts": [item["path"] for item in planned],
    }


def adopt_change(
    root: Path,
    *,
    change_id: str,
    slug: str,
    work_kind: str,
    control_level: str,
    impact_tags: list[str],
    artifacts: dict[str, str],
    ticket_statuses: dict[str, str],
    requirement_prefixes: list[str],
    operation_id: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Register a compatible existing package without modifying authored artifacts."""
    validate_change_id(change_id)
    if work_kind not in WORK_KINDS:
        raise UsageError(f"Invalid work kind: {work_kind}")
    if control_level not in CONTROL_LEVELS:
        raise UsageError(f"Invalid control level: {control_level}")
    unknown_tags = sorted(set(impact_tags) - IMPACT_TAGS)
    if unknown_tags:
        raise UsageError(f"Unknown impact tags: {', '.join(unknown_tags)}")
    load_config(root)
    safe_slug = _slugify(slug)
    if not artifacts:
        raise UsageError("Adoption requires at least one existing artifact")
    invalid_keys = sorted(
        key for key in artifacts if not ARTIFACT_KEY_PATTERN.fullmatch(key)
    )
    if invalid_keys:
        raise UsageError(f"Invalid artifact keys: {', '.join(invalid_keys)}")
    required_keys: set[str]
    if control_level == "critical":
        required_keys = {"epic", "spec", "tickets"}
    elif work_kind in {"spike", "hotfix"} or control_level == "routine":
        required_keys = {"change"}
    else:
        required_keys = {"spec"}
    missing_keys = sorted(required_keys - set(artifacts))
    if missing_keys:
        raise UsageError(
            f"Adopted {control_level} package is missing artifacts: {', '.join(missing_keys)}"
        )
    normalized_artifacts: dict[str, dict[str, str]] = {}
    for key, relative in sorted(artifacts.items()):
        path = safe_resolve(root, relative, must_exist=True)
        if not path.is_file():
            raise IntegrityError(f"Artifact is not a regular file: {relative}")
        normalized = path.relative_to(root.resolve()).as_posix()
        normalized_artifacts[key] = {"path": normalized}
    invalid_prefixes = sorted(
        prefix
        for prefix in requirement_prefixes
        if not REQUIREMENT_PREFIX_PATTERN.fullmatch(prefix)
    )
    if invalid_prefixes:
        raise UsageError(
            f"Invalid requirement prefixes: {', '.join(invalid_prefixes)}"
        )
    declared_ticket_ids: list[str] = []
    if "tickets" in normalized_artifacts:
        ticket_path = safe_resolve(
            root,
            normalized_artifacts["tickets"]["path"],
            must_exist=True,
        )
        declared_ticket_ids = TICKET_HEADING_PATTERN.findall(
            ticket_path.read_text(encoding="utf-8")
        )
        if len(declared_ticket_ids) != len(set(declared_ticket_ids)):
            raise IntegrityError("Adopted ticket artifact contains duplicate ticket IDs")
        invalid_ticket_ids = sorted(
            ticket_id
            for ticket_id in declared_ticket_ids
            if not TICKET_ID_PATTERN.fullmatch(ticket_id)
        )
        if invalid_ticket_ids:
            raise UsageError(
                f"Invalid adopted ticket IDs: {', '.join(invalid_ticket_ids)}"
            )
    unknown_ticket_ids = sorted(set(ticket_statuses) - set(declared_ticket_ids))
    missing_ticket_statuses = sorted(set(declared_ticket_ids) - set(ticket_statuses))
    invalid_ticket_statuses = sorted(
        f"{ticket_id}={ticket_status}"
        for ticket_id, ticket_status in ticket_statuses.items()
        if ticket_status not in TICKET_STATUSES
    )
    if unknown_ticket_ids:
        raise UsageError(
            f"Ticket statuses reference undeclared IDs: {', '.join(unknown_ticket_ids)}"
        )
    if missing_ticket_statuses:
        raise UsageError(
            "Adoption requires an explicit status for every ticket: "
            + ", ".join(missing_ticket_statuses)
        )
    if invalid_ticket_statuses:
        raise UsageError(
            f"Invalid adopted ticket statuses: {', '.join(invalid_ticket_statuses)}"
        )
    if "traceability" in normalized_artifacts and declared_ticket_ids:
        normalized_artifacts["traceability"]["producer_ticket_scope"] = sorted(
            declared_ticket_ids
        )
    state_store = StateStore(root)
    if state_store.path(change_id).exists():
        existing = state_store.load(change_id)
        if operation_id and _has_operation(existing, operation_id):
            existing_operation = _operation(existing, operation_id)
            assert existing_operation is not None
            _require_operation_kind(existing_operation, "state-adopt")
            return {
                "ok": True,
                "dry_run": False,
                "changed": False,
                "change_id": change_id,
                "state_revision": existing["state_revision"],
                "operation_id": operation_id,
                "artifacts": [
                    metadata["path"]
                    for _, metadata in sorted(existing["artifacts"].items())
                ],
                "tickets": existing["tickets"],
            }
        raise IntegrityError(f"Change already exists: {change_id}")
    effective_operation_id = operation_id or str(uuid.uuid4())
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "operation_id": effective_operation_id,
            "artifacts": [
                metadata["path"]
                for _, metadata in sorted(normalized_artifacts.items())
            ],
            "tickets": ticket_statuses,
        }
    state = initial_state(
        change_id=change_id,
        slug=safe_slug,
        work_kind=work_kind,
        control_level=control_level,
        impact_tags=impact_tags,
        artifacts=normalized_artifacts,
        operation_id=effective_operation_id,
    )
    state["adopted"] = True
    state["requirement_prefixes"] = sorted(set(requirement_prefixes))
    state["operations"][0]["kind"] = "state-adopt"
    adopted_at = utc_now()
    state["tickets"] = {
        ticket_id: {
            "status": ticket_statuses[ticket_id],
            "note": "Adopted from existing package.",
            "updated_at": adopted_at,
        }
        for ticket_id in declared_ticket_ids
    }
    state_store.create(state)
    return {
        "ok": True,
        "dry_run": False,
        "changed": True,
        "change_id": change_id,
        "state_revision": 1,
        "operation_id": effective_operation_id,
        "artifacts": [
            metadata["path"]
            for _, metadata in sorted(normalized_artifacts.items())
        ],
        "tickets": state["tickets"],
    }


def status(root: Path, *, change_id: str) -> dict[str, Any]:
    state = StateStore(root).load(change_id)
    definition_digest = current_definition_digest(root, state)
    approvals = derived_approval_statuses(root, state)
    head = git_head(root)
    dirty = git_source_dirty_paths(root) if is_git_repo(root) else []
    latest_review = _latest_review_result(state)
    review_stale = bool(latest_review and latest_review.get("head_sha") != head)
    return {
        "ok": True,
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "work_kind": state["work_kind"],
        "control_level": state["control_level"],
        "impact_tags": state["impact_tags"],
        "phase": state["phase"],
        "lifecycle": state["lifecycle"],
        "definition_digest": definition_digest,
        "approvals": approvals,
        "tickets": state["tickets"],
        "evidence_count": len(state["evidence"]),
        "latest_review": latest_review,
        "review_stale": review_stale,
        "git_head": head,
        "source_dirty_paths": dirty,
    }


def check(root: Path, *, change_id: str, gate: str) -> dict[str, Any]:
    if gate not in {"definition", "review", "accept", "all"}:
        raise UsageError(f"Unknown gate: {gate}")
    state = StateStore(root).load(change_id)
    checks: list[dict[str, Any]] = []
    artifact_texts: dict[str, str] = {}
    for key, metadata in sorted(state["artifacts"].items()):
        path = safe_resolve(root, metadata["path"])
        exists = path.is_file()
        checks.append(_check(f"artifact:{key}:exists", exists, metadata["path"]))
        if not exists:
            continue
        text = path.read_text(encoding="utf-8")
        artifact_texts[key] = text
        unresolved = "{{" in text or "[TODO" in text
        checks.append(_check(f"artifact:{key}:resolved", not unresolved, "no template tokens"))
    declared_ticket_ids = TICKET_HEADING_PATTERN.findall(
        artifact_texts.get("tickets", "")
    )
    if "tickets" in state["artifacts"]:
        checks.append(
            _check(
                "tickets:unique-ids",
                len(declared_ticket_ids) == len(set(declared_ticket_ids)),
                ",".join(declared_ticket_ids) or "none",
            )
        )
    requirement_pattern = _requirement_id_pattern(state)
    requirements: set[str] = set()
    ticket_requirement_links: set[str] = set()
    for key, text in artifact_texts.items():
        ids = set(requirement_pattern.findall(text))
        if key == "tickets":
            ticket_requirement_links.update(ids)
        elif key == "traceability":
            try:
                ticket_requirement_links.update(
                    _traceability_requirement_ids(
                        text,
                        declared_ticket_ids=set(declared_ticket_ids),
                        requirement_pattern=requirement_pattern,
                    )
                )
                checks.append(_check("traceability:parse", True, "valid JSON"))
            except json.JSONDecodeError as exc:
                checks.append(
                    _check(
                        "traceability:parse",
                        False,
                        f"invalid JSON at line {exc.lineno} column {exc.colno}",
                    )
                )
        else:
            requirements.update(ids)
    if "tickets" in state["artifacts"]:
        missing_links = sorted(requirements - ticket_requirement_links)
        checks.append(
            _check(
                "traceability:requirements-to-tickets",
                not missing_links,
                "missing=" + ",".join(missing_links) if missing_links else "covered",
            )
        )
    approvals = derived_approval_statuses(root, state)
    definition_approved = any(
        item.get("decision") == "definition" and item.get("status") == "current"
        for item in approvals
    )
    design_approved = any(
        item.get("decision") == "design" and item.get("status") == "current"
        for item in approvals
    )
    architecture_approved = any(
        item.get("decision") == "architecture" and item.get("status") == "current"
        for item in approvals
    )
    latest_review = _latest_review_result(state)
    review_clear = bool(
        latest_review
        and latest_review.get("verdict") == "review-clear"
        and latest_review.get("head_sha") == git_head(root)
    )
    acceptance_grade_review = bool(
        review_clear and latest_review and latest_review.get("mode") == "acceptance-grade"
    )
    source_dirty = git_source_dirty_paths(root) if is_git_repo(root) else ["not-a-git-repository"]
    strict_path = state["control_level"] in {"standard", "critical"}
    if gate in {"review", "accept", "all"}:
        if strict_path:
            checks.append(
                _check("definition:approved", definition_approved, "current approval required")
            )
        if "user-interface" in state["impact_tags"]:
            checks.append(
                _check(
                    "ui:design-decision",
                    design_approved,
                    "accepted source or explicit bypass decision required",
                )
            )
        if "adr" in state["artifacts"]:
            checks.append(
                _check(
                    "architecture:decision",
                    architecture_approved,
                    "current architecture decision required for ADR",
                )
            )
        if strict_path:
            checks.append(
                _check("git:source-clean", not source_dirty, ",".join(source_dirty) or "clean")
            )
            review_ticket_statuses = {
                ticket_id: state["tickets"].get(ticket_id, {}).get("status")
                for ticket_id in declared_ticket_ids
            }
            incomplete_review_tickets = sorted(
                ticket_id
                for ticket_id, ticket_status in review_ticket_statuses.items()
                if ticket_status not in {"implemented", "validated", "done"}
            )
            checks.append(
                _check(
                    "tickets:implemented-for-review",
                    not incomplete_review_tickets,
                    "incomplete=" + ",".join(incomplete_review_tickets)
                    if incomplete_review_tickets
                    else "ready",
                )
            )
            evidence_ok, evidence_detail = _successful_evidence_for_current_revision(
                root,
                state,
                stage="review",
            )
            checks.append(_check("validation:passing-evidence", evidence_ok, evidence_detail))
    if gate in {"accept", "all"}:
        if strict_path:
            checks.append(
                _check(
                    "review:clear",
                    acceptance_grade_review,
                    "current acceptance-grade review-clear required",
                )
            )
        elif latest_review:
            checks.append(
                _check(
                    "review:optional-result-clear",
                    review_clear,
                    "performed optional review must be current and clear",
                )
            )
        if not strict_path:
            evidence_ok, evidence_detail = _successful_evidence_for_current_revision(
                root,
                state,
                stage="acceptance",
            )
            checks.append(_check("validation:passing-evidence", evidence_ok, evidence_detail))
        if strict_path:
            evidence_ok, evidence_detail = _successful_evidence_for_current_revision(
                root,
                state,
                stage="acceptance",
            )
            checks.append(
                _check(
                    "validation:acceptance-evidence",
                    evidence_ok,
                    evidence_detail,
                )
            )
            acceptance_ticket_statuses = {
                ticket_id: state["tickets"].get(ticket_id, {}).get("status")
                for ticket_id in declared_ticket_ids
            }
            unvalidated_tickets = sorted(
                ticket_id
                for ticket_id, ticket_status in acceptance_ticket_statuses.items()
                if ticket_status not in {"validated", "done"}
            )
            checks.append(
                _check(
                    "tickets:validated-for-acceptance",
                    not unvalidated_tickets,
                    "unvalidated=" + ",".join(unvalidated_tickets)
                    if unvalidated_tickets
                    else "validated",
                )
            )
        blockers = _open_finding_counts(root, state)
        checks.append(
            _check(
                "findings:no-open-blockers",
                blockers["blocker"] == 0,
                json.dumps(blockers, sort_keys=True),
            )
        )
        checks.append(
            _check(
                "findings:no-unaccepted-should-fix",
                blockers["should-fix"] == 0,
                json.dumps(blockers, sort_keys=True),
            )
        )
    return {
        "ok": all(item["ok"] for item in checks),
        "gate": gate,
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "checks": checks,
    }


def approve(
    root: Path,
    *,
    change_id: str,
    decision: str,
    expected_revision: int,
    actor: str,
    prompt: str | None,
    response: str | None,
    git_sha: str | None,
    conditions: str | None,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if decision not in {"definition", "accept", "exception", "design", "architecture"}:
        raise UsageError(f"Invalid approval decision: {decision}")
    if actor not in {"codex", "user"}:
        raise UsageError("actor must be codex or user")
    state_store = StateStore(root)
    state = state_store.load(change_id)
    effective_operation_id = operation_id or str(uuid.uuid4())
    operation_kind = f"approve:{decision}"
    approval_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"dls:{change_id}:approval:{decision}:{effective_operation_id}",
        )
    )
    existing_operation = _operation(state, effective_operation_id)
    if existing_operation:
        _require_operation_kind(existing_operation, operation_kind)
        recorded = next(
            (item for item in state["approvals"] if item.get("id") == approval_id),
            None,
        )
        if not recorded:
            raise IntegrityError(f"Approval operation has no matching record: {effective_operation_id}")
        return {
            "ok": True,
            "dry_run": False,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "approval": recorded,
        }
    _require_revision(state, expected_revision)
    if state["lifecycle"] == "accepted" and decision != "exception":
        raise IntegrityError("Accepted work cannot receive another decision without a new change")
    object_digest = current_definition_digest(root, state)
    current_head = git_head(root)
    if decision == "accept":
        strict_path = state["control_level"] in {"standard", "critical"}
        if strict_path and not current_head:
            raise IntegrityError("Standard and critical acceptance requires Git")
        if git_sha and git_sha != current_head:
            raise IntegrityError(f"Acceptance SHA is not current HEAD: {git_sha} != {current_head}")
        git_sha = current_head
        dirty = git_source_dirty_paths(root) if current_head else []
        if strict_path and dirty:
            raise IntegrityError(f"Acceptance requires clean product source: {', '.join(dirty)}")
        gate = check(root, change_id=change_id, gate="accept")
        if not gate["ok"] and decision != "exception":
            failed = [item["id"] for item in gate["checks"] if not item["ok"]]
            raise IntegrityError(f"Acceptance gate failed: {', '.join(failed)}")
    if actor == "codex":
        _validate_scoped_confirmation(decision, object_digest, prompt, response)
    approval = {
        "id": approval_id,
        "decision": decision,
        "object_digest": object_digest,
        "git_sha": git_sha,
        "actor": actor,
        "authority": "user",
        "recorded_at": utc_now(),
        "status": "current",
        "conditions": conditions,
        "prompt": prompt,
        "response": response,
    }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "approval": approval,
        }

    def mutate(value: dict[str, Any]) -> None:
        for existing in value["approvals"]:
            if existing.get("decision") == decision and existing.get("status") == "current":
                existing["status"] = "superseded"
                existing["superseded_by"] = approval_id
        value["approvals"].append(approval)
        if decision == "definition":
            value["phase"] = "implementation"
            value["lifecycle"] = "approved"
        elif decision == "accept":
            value["phase"] = "accepted"
            value["lifecycle"] = "accepted"

    updated, changed = state_store.mutate(
        change_id,
        expected_revision=expected_revision,
        operation_id=effective_operation_id,
        operation_kind=operation_kind,
        mutator=mutate,
    )
    recorded_approval = next(
        (item for item in updated["approvals"] if item.get("id") == approval_id),
        approval,
    )
    return {
        "ok": True,
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "operation_id": effective_operation_id,
        "approval": recorded_approval,
    }


def revoke_approval(
    root: Path,
    *,
    change_id: str,
    approval_id: str,
    expected_revision: int,
    actor: str,
    prompt: str | None,
    response: str | None,
    rationale: str,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if actor not in {"codex", "user"}:
        raise UsageError("actor must be codex or user")
    if not rationale.strip():
        raise UsageError("Approval revocation requires a rationale")
    state_store = StateStore(root)
    state = state_store.load(change_id)
    effective_operation_id = operation_id or str(uuid.uuid4())
    operation_kind = f"approval-revoke:{approval_id}"
    revocation_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"dls:{change_id}:approval-revoke:{approval_id}:{effective_operation_id}",
        )
    )
    existing_operation = _operation(state, effective_operation_id)
    if existing_operation:
        _require_operation_kind(existing_operation, operation_kind)
        recorded = next(
            (item for item in state["approvals"] if item.get("id") == revocation_id),
            None,
        )
        if not recorded:
            raise IntegrityError(
                f"Revocation operation has no matching record: {effective_operation_id}"
            )
        return {
            "ok": True,
            "dry_run": False,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "approval": recorded,
        }
    _require_revision(state, expected_revision)
    target = next(
        (item for item in state["approvals"] if item.get("id") == approval_id),
        None,
    )
    if not target:
        raise IntegrityError(f"Unknown approval: {approval_id}")
    if target.get("status") not in {"current", "stale"}:
        raise IntegrityError(f"Approval is not revocable from status {target.get('status')}")
    binding_digest = str(target.get("object_digest") or current_definition_digest(root, state))
    if actor == "codex":
        _validate_scoped_confirmation(f"revoke {approval_id}", binding_digest, prompt, response)
    revocation = {
        "id": revocation_id,
        "decision": "revoke",
        "target_approval_id": approval_id,
        "object_digest": binding_digest,
        "git_sha": target.get("git_sha"),
        "actor": actor,
        "authority": "user",
        "recorded_at": utc_now(),
        "status": "current",
        "conditions": rationale,
        "prompt": prompt,
        "response": response,
    }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "approval": revocation,
        }

    def mutate(value: dict[str, Any]) -> None:
        mutable_target = next(
            item for item in value["approvals"] if item.get("id") == approval_id
        )
        mutable_target["status"] = "revoked"
        mutable_target["revoked_by"] = revocation_id
        value["approvals"].append(revocation)
        if mutable_target.get("decision") == "definition":
            value["phase"] = "definition"
            value["lifecycle"] = "draft"
        elif mutable_target.get("decision") == "accept":
            value["phase"] = "review"
            value["lifecycle"] = "review-clear"

    updated, changed = state_store.mutate(
        change_id,
        expected_revision=expected_revision,
        operation_id=effective_operation_id,
        operation_kind=operation_kind,
        mutator=mutate,
    )
    recorded = next(
        (item for item in updated["approvals"] if item.get("id") == revocation_id),
        revocation,
    )
    return {
        "ok": True,
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "operation_id": effective_operation_id,
        "approval": recorded,
    }


def ticket_set(
    root: Path,
    *,
    change_id: str,
    ticket_id: str,
    ticket_status: str,
    expected_revision: int,
    note: str | None,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not TICKET_ID_PATTERN.fullmatch(ticket_id):
        raise UsageError("Ticket ID must match T followed by at least two digits")
    if ticket_status not in TICKET_STATUSES:
        raise UsageError(f"Invalid ticket status: {ticket_status}")
    state_store = StateStore(root)
    state = state_store.load(change_id)
    effective_operation_id = operation_id or str(uuid.uuid4())
    operation_kind = f"ticket-set:{ticket_id}:{ticket_status}"
    existing_operation = _operation(state, effective_operation_id)
    if existing_operation:
        _require_operation_kind(existing_operation, operation_kind)
        ticket = state["tickets"].get(ticket_id)
        if not isinstance(ticket, dict):
            raise IntegrityError(f"Ticket operation has no matching state: {effective_operation_id}")
        return {
            "ok": True,
            "dry_run": False,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "ticket_id": ticket_id,
            "ticket": ticket,
        }
    _require_revision(state, expected_revision)
    current_status = state["tickets"].get(ticket_id, {}).get("status")
    allowed = TICKET_TRANSITIONS.get(current_status, set())
    if ticket_status != current_status and ticket_status not in allowed:
        raise IntegrityError(
            f"Illegal ticket transition for {ticket_id}: {current_status or 'unset'} -> {ticket_status}"
        )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "ticket_id": ticket_id,
            "ticket": {
                "status": ticket_status,
                "note": note,
            },
        }

    def mutate(value: dict[str, Any]) -> None:
        value["tickets"][ticket_id] = {
            "status": ticket_status,
            "note": note,
            "updated_at": utc_now(),
        }

    updated, changed = state_store.mutate(
        change_id,
        expected_revision=expected_revision,
        operation_id=effective_operation_id,
        operation_kind=operation_kind,
        mutator=mutate,
    )
    return {
        "ok": True,
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "operation_id": effective_operation_id,
        "ticket_id": ticket_id,
        "ticket": updated["tickets"][ticket_id],
    }


def _normalized_disposition_status(status: Any) -> str | None:
    if status == "resolved":
        return "addressed"
    return status if isinstance(status, str) and status in DISPOSITION_STATUSES else None


def _latest_dispositions(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for item in state["finding_dispositions"]:
        finding_id = item.get("finding_id")
        status = _normalized_disposition_status(item.get("status"))
        if isinstance(finding_id, str) and status:
            normalized = dict(item)
            normalized["status"] = status
            if item.get("status") == "resolved":
                normalized["legacy_status"] = "resolved"
            output[finding_id] = normalized
    return output


def _evidence_records(
    root: Path,
    state: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    output: list[tuple[str, dict[str, Any]]] = []
    for relative in state["evidence"]:
        try:
            record = read_json(safe_resolve(root, relative, must_exist=True))
        except IntegrityError:
            continue
        if isinstance(record, dict):
            output.append((relative, record))
    return output


def _current_evidence_by_command(
    root: Path,
    state: dict[str, Any],
) -> dict[str, tuple[str, dict[str, Any]]]:
    current_head = git_head(root)
    current_source_digest = git_source_snapshot_digest(root)
    latest: dict[str, tuple[str, dict[str, Any]]] = {}
    for relative, record in _evidence_records(root, state):
        command_id = record.get("command_id")
        if (
            not isinstance(command_id, str)
            or record.get("git_sha") != current_head
            or record.get("source_digest") != current_source_digest
        ):
            continue
        latest[command_id] = (relative, record)
    return latest


def _current_successful_evidence_paths(
    root: Path,
    state: dict[str, Any],
) -> list[str]:
    latest = _current_evidence_by_command(root, state)
    output: list[str] = []
    for relative, record in latest.values():
        extra = record.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        if (
            record.get("exit_code") == 0
            and not extra.get("timed_out", False)
            and not extra.get("output_overflow", False)
        ):
            output.append(relative)
    return sorted(output)


def _required_evidence_status(
    root: Path,
    state: dict[str, Any],
    *,
    stage: str,
) -> tuple[bool, str, list[str]]:
    config = load_config(root)
    policy = config.get("policy", {})
    key = (
        "review_required_commands"
        if stage == "review"
        else "acceptance_required_commands"
    )
    required = list(policy.get(key, []))
    latest = _current_evidence_by_command(root, state)
    successful: dict[str, str] = {}
    for command_id, (relative, record) in latest.items():
        extra = record.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        if (
            record.get("exit_code") == 0
            and not extra.get("timed_out", False)
            and not extra.get("output_overflow", False)
        ):
            successful[command_id] = relative
    if required:
        missing = [command_id for command_id in required if command_id not in successful]
        return (
            not missing,
            "missing=" + ",".join(missing) if missing else "required commands current",
            [successful[command_id] for command_id in required if command_id in successful],
        )
    paths = sorted(successful.values())
    return (
        bool(paths),
        f"passing_current={len(paths)}; compatibility_minimum=1",
        paths,
    )


def _read_review_result(
    root: Path,
    entry: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    relative = entry.get("result_path")
    if not isinstance(relative, str):
        raise IntegrityError("Review result state entry has no result_path")
    path = safe_resolve(root, relative)
    report = read_json(path)
    digest = sha256_bytes(
        json.dumps(
            report,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    recorded = entry.get("result_digest")
    if isinstance(recorded, str) and recorded != digest:
        raise IntegrityError("Review result digest does not match DLS state")
    return relative, report


def _all_review_findings(
    root: Path,
    state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    findings: dict[str, dict[str, Any]] = {}
    for entry in state["reviews"]:
        if entry.get("kind") != "result":
            continue
        _, report = _read_review_result(root, entry)
        for finding in report.get("findings", []):
            finding_id = finding.get("id")
            if isinstance(finding_id, str):
                findings[finding_id] = finding
    return findings


def _superseded_prior_finding_ids(
    root: Path,
    state: dict[str, Any],
) -> set[str]:
    output: set[str] = set()
    for entry in state["reviews"]:
        if entry.get("kind") != "result":
            continue
        _, report = _read_review_result(root, entry)
        for verdict in report.get("prior_finding_verdicts", []):
            if (
                verdict.get("verdict") in {"still-open", "regressed"}
                and isinstance(verdict.get("replacement_finding_id"), str)
            ):
                finding_id = verdict.get("finding_id")
                if isinstance(finding_id, str):
                    output.add(finding_id)
    return output


def _active_prior_findings(
    root: Path,
    state: dict[str, Any],
    *,
    include_waived: bool = False,
) -> list[dict[str, Any]]:
    dispositions = _latest_dispositions(state)
    superseded = _superseded_prior_finding_ids(root, state)
    output: list[dict[str, Any]] = []
    for finding_id, finding in sorted(_all_review_findings(root, state).items()):
        if (
            finding_id in superseded
            or finding.get("severity") not in {"blocker", "should-fix"}
            or "review" not in _finding_blocks(finding)
        ):
            continue
        disposition = dispositions.get(finding_id)
        if disposition and disposition["status"] == "verified":
            continue
        if (
            disposition
            and disposition["status"] == "waived"
            and not include_waived
        ):
            continue
        output.append(
            {
                "finding_id": finding_id,
                "severity": finding["severity"],
                "kind": finding["kind"],
                "ticket_ids": finding.get("ticket_ids", []),
                "requirement_ids": finding.get("requirement_ids", []),
                "blocks": sorted(_finding_blocks(finding)),
                "location": finding.get("location"),
                "issue": finding.get("issue"),
                "required_fix": finding.get("required_fix"),
                "disposition": disposition,
            }
        )
    return output


def _risk_lenses(state: dict[str, Any]) -> list[dict[str, Any]]:
    if state["control_level"] != "critical":
        return []
    impact_tags = set(state["impact_tags"])
    ranked: list[tuple[int, int, dict[str, Any], list[str]]] = []
    for definition in RISK_LENS_DEFINITIONS:
        matched = sorted(impact_tags & definition["tags"])
        if matched:
            ranked.append(
                (-len(matched), definition["priority"], definition, matched)
            )
    output: list[dict[str, Any]] = []
    for _, _, definition, matched in sorted(ranked)[:3]:
        output.append(
            {
                "id": definition["id"],
                "impact_tags": matched,
                "focus": definition["focus"],
            }
        )
    return output


def _blast_radius_triggers(findings: list[dict[str, Any]]) -> list[str]:
    triggers: set[str] = set()
    for finding in findings:
        kind = finding.get("kind")
        text = " ".join(
            str(finding.get(field, ""))
            for field in ("location", "issue", "impact", "required_fix")
        ).lower()
        if kind == "governance":
            triggers.add("definition-and-governance-consistency")
        if kind == "validation-gap":
            triggers.add("exact-head-validation-evidence")
        if any(token in text for token in ("protocol", "json", "sse", "http", "compatib")):
            triggers.add("protocol-and-backward-compatibility")
        if any(token in text for token in ("concurr", "actor", "task", "cancel", "race", "deadline")):
            triggers.add("concurrency-cancellation-and-failure-interleavings")
        if any(token in text for token in ("security", "auth", "secret", "trust")):
            triggers.add("security-and-trust-boundary")
        if any(token in text for token in ("migration", "ledger", "persist", "data")):
            triggers.add("data-integrity-and-migration")
    return sorted(triggers)


def build_context(
    root: Path,
    *,
    change_id: str,
    phase: str,
    include: list[str],
    exclude: list[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    if phase not in {"implementation", "review", "remediation"}:
        raise UsageError(f"Invalid context phase: {phase}")
    state = StateStore(root).load(change_id)
    profile = load_config(root)["default_profile"]
    selected: dict[str, str] = {}
    required_paths: set[str] = set()
    for key, metadata in state["artifacts"].items():
        selected[metadata["path"]] = f"canonical-{key}"
        required_paths.add(metadata["path"])
    config_path = ".dls/config.toml"
    selected[config_path] = "repository-dls-config"
    required_paths.add(config_path)
    agents_path = root / "AGENTS.md"
    if agents_path.is_file():
        selected["AGENTS.md"] = "repository-rules"
        required_paths.add("AGENTS.md")
    if phase in {"review", "remediation"}:
        for evidence_path in _current_successful_evidence_paths(root, state):
            selected[evidence_path] = "validation-evidence"
            required_paths.add(evidence_path)
    if phase == "review":
        completed_review_ids = {
            entry.get("review_id")
            for entry in state["reviews"]
            if entry.get("kind") == "result"
        }
        pending_pack = next(
            (
                entry
                for entry in reversed(state["reviews"])
                if entry.get("kind") == "pack"
                and entry.get("review_id") not in completed_review_ids
                and isinstance(entry.get("pack_path"), str)
            ),
            None,
        )
        if pending_pack:
            pack_path = pending_pack["pack_path"]
            selected[pack_path] = "active-review-pack"
            required_paths.add(pack_path)
    if phase == "remediation":
        latest_result = _latest_review_result(state)
        if latest_result:
            path = latest_result.get("result_path")
            if isinstance(path, str):
                selected[path] = "latest-review-findings"
                required_paths.add(path)
    for item in include:
        relative, _, reason = item.partition(":")
        selected[relative] = reason or "explicit-include"
    excluded = set(exclude)
    forbidden_exclusions = sorted(excluded & required_paths)
    if forbidden_exclusions:
        raise IntegrityError(
            "Required context inputs cannot be excluded: " + ", ".join(forbidden_exclusions)
        )
    inputs: list[dict[str, Any]] = []
    for relative, reason in sorted(selected.items()):
        if relative in excluded:
            continue
        path = safe_resolve(root, relative, must_exist=True)
        stat = path.stat()
        words = 0
        try:
            words = len(path.read_text(encoding="utf-8").split())
        except UnicodeDecodeError:
            pass
        inputs.append(
            {
                "path": relative,
                "reason": reason,
                "sha256": sha256_file(path),
                "canonical_sha256": canonical_file_digest(path),
                "bytes": stat.st_size,
                "words": words,
                "estimated_tokens": {
                    "low": max(1, int(words * 1.1)) if words else 0,
                    "high": max(1, int(words * 1.8)) if words else 0,
                },
            }
        )
    head = git_head(root)
    digest_basis = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "dls_version": VERSION,
            "profile": profile,
            "change_id": change_id,
            "phase": phase,
            "state_revision": state["state_revision"],
            "git_head": head,
            "inputs": inputs,
            "exclusions": sorted(excluded),
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    manifest_digest = sha256_bytes(digest_basis)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dls_version": VERSION,
        "profile": profile,
        "change_id": change_id,
        "phase": phase,
        "generated_at": utc_now(),
        "manifest_digest": manifest_digest,
        "state_revision": state["state_revision"],
        "git_head": head,
        "inputs": inputs,
        "exclusions": sorted(excluded),
        "totals": {
            "bytes": sum(item["bytes"] for item in inputs),
            "words": sum(item["words"] for item in inputs),
            "estimated_tokens_low": sum(item["estimated_tokens"]["low"] for item in inputs),
            "estimated_tokens_high": sum(item["estimated_tokens"]["high"] for item in inputs),
        },
        "largest_inputs": sorted(
            (
                {"path": item["path"], "bytes": item["bytes"]}
                for item in inputs
            ),
            key=lambda item: (-item["bytes"], item["path"]),
        )[:5],
        "warnings": [
            f"Large context input: {item['path']} ({item['bytes']} bytes)"
            for item in inputs
            if item["bytes"] > 262144
        ],
    }
    output = root / ".dls" / "cache" / "context" / change_id / f"{phase}-{manifest_digest[:12]}.json"
    if not dry_run:
        atomic_write_json(output, manifest, backup=False)
    return {
        "ok": True,
        "dry_run": dry_run,
        "change_id": change_id,
        "phase": phase,
        "manifest_path": None if dry_run else str(output.relative_to(root)),
        "manifest": manifest,
    }


def remediation_start(
    root: Path,
    *,
    change_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    state = StateStore(root).load(change_id)
    latest = _latest_review_result(state)
    if not latest:
        raise IntegrityError("Remediation requires an imported ReviewIR")
    result_path, report = _read_review_result(root, latest)
    current_head = git_head(root)
    if report.get("head_sha") != current_head:
        raise IntegrityError(
            "Latest ReviewIR is stale for remediation: "
            f"{report.get('head_sha')} != {current_head}"
        )
    current_definition = current_definition_digest(root, state)
    if report.get("definition_digest") != current_definition:
        raise IntegrityError("Latest ReviewIR definition digest is stale")
    current_approval = next(
        (
            item
            for item in reversed(derived_approval_statuses(root, state))
            if item.get("decision") == "definition"
            and item.get("status") == "current"
            and item.get("object_digest") == current_definition
        ),
        None,
    )
    if state["control_level"] in {"standard", "critical"} and not current_approval:
        raise IntegrityError("Remediation requires a current definition approval")
    if git_source_dirty_paths(root):
        raise IntegrityError("Remediation must start from a clean product source")
    open_findings = _active_prior_findings(root, state)
    if not open_findings:
        raise IntegrityError("Latest ReviewIR has no open review findings to remediate")
    inputs: list[dict[str, Any]] = []
    input_paths = [
        metadata["path"]
        for _, metadata in sorted(state["artifacts"].items())
    ]
    input_paths.append(result_path)
    input_paths.extend(_current_successful_evidence_paths(root, state))
    for relative in dict.fromkeys(input_paths):
        path = safe_resolve(root, relative, must_exist=True)
        inputs.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "canonical_sha256": canonical_file_digest(path),
                "bytes": path.stat().st_size,
            }
        )
    affected_paths: set[str] = set()
    for finding in open_findings:
        location = finding.get("location")
        if isinstance(location, str):
            candidate = location.split(":", 1)[0]
            try:
                safe_resolve(root, candidate, must_exist=True)
            except IntegrityError:
                continue
            affected_paths.add(candidate)
    manifest = {
        "schema_version": 2,
        "dls_version": VERSION,
        "change_id": change_id,
        "review_id": report["review_id"],
        "review_result_path": result_path,
        "review_result_digest": latest.get("result_digest")
        or sha256_bytes(
            json.dumps(
                report,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "reviewed_head_sha": report["head_sha"],
        "definition_digest": report["definition_digest"],
        "source_snapshot_digest": latest.get("source_snapshot_digest"),
        "open_findings": open_findings,
        "ticket_ids": sorted(
            {
                ticket_id
                for finding in open_findings
                for ticket_id in finding["ticket_ids"]
            }
        ),
        "requirement_ids": sorted(
            {
                requirement_id
                for finding in open_findings
                for requirement_id in finding["requirement_ids"]
            }
        ),
        "affected_paths": sorted(affected_paths),
        "blast_radius_triggers": _blast_radius_triggers(open_findings),
        "inputs": inputs,
    }
    manifest["manifest_digest"] = sha256_bytes(
        json.dumps(
            manifest,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    relative_path = (
        f".dls/cache/context/{change_id}/"
        f"remediation-{report['review_id']}.json"
    )
    output_path = safe_resolve(root, relative_path)
    existed = output_path.is_file()
    if not dry_run:
        write_immutable_json(output_path, manifest)
    return {
        "ok": True,
        "dry_run": dry_run,
        "changed": not dry_run and not existed,
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "review_id": report["review_id"],
        "remediation_manifest_path": None if dry_run else relative_path,
        "remediation_manifest": manifest,
    }


def evidence_add(
    root: Path,
    *,
    change_id: str,
    command_id: str,
    exit_code: int,
    summary: str,
    expected_revision: int,
    git_sha: str | None,
    artifacts: list[str],
    environment: str | None,
    duration_seconds: float | None,
    operation_id: str | None,
    extra: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_store = StateStore(root)
    state = state_store.load(change_id)
    effective_operation_id = operation_id or str(uuid.uuid4())
    operation_kind = f"evidence-add:{command_id}"
    evidence_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"dls:{change_id}:evidence:{effective_operation_id}",
        )
    )
    relative_path = f".dls/evidence/{change_id}/{evidence_id}.json"
    existing_operation = _operation(state, effective_operation_id)
    if existing_operation:
        _require_operation_kind(existing_operation, operation_kind)
        record = read_json(safe_resolve(root, relative_path, must_exist=True))
        return {
            "ok": record["exit_code"] == 0,
            "dry_run": False,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "evidence_path": relative_path,
            "evidence": record,
        }
    _require_revision(state, expected_revision)
    current_head = git_head(root)
    if git_sha and current_head and git_sha != current_head:
        raise IntegrityError(f"Evidence SHA is not current HEAD: {git_sha} != {current_head}")
    artifact_records: list[dict[str, Any]] = []
    for relative in artifacts:
        path = safe_resolve(root, relative, must_exist=True)
        artifact_records.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": evidence_id,
        "change_id": change_id,
        "command_id": command_id,
        "recorded_at": utc_now(),
        "git_sha": git_sha or current_head,
        "source_digest": git_source_snapshot_digest(root),
        "exit_code": exit_code,
        "summary": redact_text(summary),
        "environment": redact_text(environment) if environment else None,
        "duration_seconds": duration_seconds,
        "artifacts": artifact_records,
    }
    if extra:
        record["extra"] = extra
    if dry_run:
        return {
            "ok": exit_code == 0,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "evidence_path": None,
            "evidence": record,
        }
    write_immutable_json(safe_resolve(root, relative_path), record)

    def mutate(value: dict[str, Any]) -> None:
        value["evidence"].append(relative_path)

    updated, changed = state_store.mutate(
        change_id,
        expected_revision=expected_revision,
        operation_id=effective_operation_id,
        operation_kind=operation_kind,
        mutator=mutate,
    )
    return {
        "ok": exit_code == 0,
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "operation_id": effective_operation_id,
        "evidence_path": relative_path,
        "evidence": record,
    }


def _review_pack_digest(pack: dict[str, Any]) -> str:
    digest_basis = {
        key: value for key, value in pack.items() if key != "pack_digest"
    }
    payload = json.dumps(
        digest_basis,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(payload)


def _review_pack_required_lanes(
    *,
    control_level: str,
    mode: str,
) -> list[str]:
    lanes = ["semantic-dls"]
    if mode == "acceptance-grade" and control_level in {"standard", "critical"}:
        lanes.insert(0, "native-diff")
    return lanes


def _validate_review_pack(pack: dict[str, Any], change_id: str) -> None:
    required = {
        "schema_version",
        "review_id",
        "change_id",
        "mode",
        "control_level",
        "created_at",
        "base_sha",
        "head_sha",
        "merge_base",
        "definition_digest",
        "source_snapshot_digest",
        "pack_digest",
        "required_lanes",
        "previous_pack",
        "state_revision",
        "artifacts",
        "tickets",
        "evidence",
        "changed_files",
        "source_dirty_paths",
    }
    missing = sorted(required - pack.keys())
    if missing:
        raise IntegrityError(f"ReviewPack missing fields: {', '.join(missing)}")
    schema_version = pack.get("schema_version")
    if schema_version not in {1, REVIEW_PACK_SCHEMA_VERSION} or pack["change_id"] != change_id:
        raise IntegrityError("ReviewPack schema or change_id mismatch")
    if pack["mode"] not in {"acceptance-grade", "advisory-dirty"}:
        raise IntegrityError(f"Invalid ReviewPack mode: {pack['mode']!r}")
    if pack["control_level"] not in CONTROL_LEVELS:
        raise IntegrityError(f"Invalid ReviewPack control level: {pack['control_level']!r}")
    for field in (
        "review_id",
        "created_at",
        "base_sha",
        "head_sha",
        "merge_base",
        "definition_digest",
        "source_snapshot_digest",
        "pack_digest",
    ):
        if not isinstance(pack.get(field), str) or not pack[field]:
            raise IntegrityError(f"ReviewPack field must be a non-empty string: {field}")
    lanes = pack["required_lanes"]
    expected_lanes = _review_pack_required_lanes(
        control_level=pack["control_level"],
        mode=pack["mode"],
    )
    if lanes != expected_lanes:
        raise IntegrityError(
            f"ReviewPack lanes mismatch: expected {expected_lanes}, got {lanes!r}"
        )
    for field in ("evidence", "changed_files", "source_dirty_paths"):
        if not isinstance(pack.get(field), list) or not all(
            isinstance(item, str) for item in pack[field]
        ):
            raise IntegrityError(f"ReviewPack field must be a string array: {field}")
    if not isinstance(pack["tickets"], dict) or not isinstance(pack["artifacts"], dict):
        raise IntegrityError("ReviewPack artifacts and tickets must be objects")
    if schema_version == REVIEW_PACK_SCHEMA_VERSION:
        v2_required = {
            "review_mode",
            "epic_base_sha",
            "comparison_base_sha",
            "epic_merge_base",
            "prior_review",
            "remediation_manifest",
            "risk_lenses",
            "required_prior_findings",
            "prior_native_coverage",
            "full_changed_files",
        }
        missing_v2 = sorted(v2_required - pack.keys())
        if missing_v2:
            raise IntegrityError(
                "ReviewPack v2 missing fields: " + ", ".join(missing_v2)
            )
        if pack["review_mode"] not in REVIEW_MODES:
            raise IntegrityError(f"Invalid ReviewPack review_mode: {pack['review_mode']!r}")
        if pack["base_sha"] != pack["epic_base_sha"]:
            raise IntegrityError("ReviewPack v2 base_sha must equal epic_base_sha")
        if pack["review_mode"] == "full":
            if pack["comparison_base_sha"] != pack["epic_base_sha"]:
                raise IntegrityError("Full ReviewPack must compare from epic_base_sha")
            if pack["prior_review"] is not None or pack["remediation_manifest"] is not None:
                raise IntegrityError("Full ReviewPack cannot bind remediation inputs")
            if pack["required_prior_findings"]:
                raise IntegrityError("Full ReviewPack cannot require prior findings")
        else:
            if not isinstance(pack["prior_review"], dict):
                raise IntegrityError("Remediation ReviewPack requires prior_review")
            if not isinstance(pack["remediation_manifest"], dict):
                raise IntegrityError("Remediation ReviewPack requires remediation_manifest")
            if (
                pack["comparison_base_sha"]
                != pack["prior_review"].get("head_sha")
            ):
                raise IntegrityError(
                    "Remediation comparison_base_sha must equal prior reviewed HEAD"
                )
        if not isinstance(pack["risk_lenses"], list) or len(pack["risk_lenses"]) > 3:
            raise IntegrityError("ReviewPack risk_lenses must contain at most three lanes")
        if not isinstance(pack["required_prior_findings"], list):
            raise IntegrityError("ReviewPack required_prior_findings must be an array")
        prior_ids = [
            item.get("finding_id")
            for item in pack["required_prior_findings"]
            if isinstance(item, dict)
        ]
        if (
            len(prior_ids) != len(pack["required_prior_findings"])
            or not all(isinstance(item, str) and item for item in prior_ids)
            or len(prior_ids) != len(set(prior_ids))
        ):
            raise IntegrityError("ReviewPack required prior finding IDs are invalid")
        if not isinstance(pack["prior_native_coverage"], list):
            raise IntegrityError("ReviewPack prior_native_coverage must be an array")
        if not isinstance(pack["full_changed_files"], list):
            raise IntegrityError("ReviewPack full_changed_files must be an array")
    if pack["pack_digest"] != _review_pack_digest(pack):
        raise IntegrityError("ReviewPack content digest mismatch")


def _previous_pack_link(root: Path, state: dict[str, Any]) -> dict[str, str] | None:
    entry = next(
        (
            item
            for item in reversed(state["reviews"])
            if item.get("kind") == "pack"
            and isinstance(item.get("review_id"), str)
            and isinstance(item.get("pack_path"), str)
        ),
        None,
    )
    if not entry:
        return None
    path = safe_resolve(root, entry["pack_path"], must_exist=True)
    digest = entry.get("pack_digest")
    if not isinstance(digest, str) or not digest:
        digest = sha256_file(path)
    return {
        "review_id": entry["review_id"],
        "pack_path": entry["pack_path"],
        "pack_digest": digest,
    }


def _prior_review_link(
    root: Path,
    state: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    entry = _latest_review_result(state)
    if not entry:
        return None, None
    relative, report = _read_review_result(root, entry)
    return (
        {
            "review_id": report["review_id"],
            "result_path": relative,
            "result_digest": entry.get("result_digest")
            or sha256_file(safe_resolve(root, relative, must_exist=True)),
            "head_sha": report["head_sha"],
            "definition_digest": report["definition_digest"],
        },
        report,
    )


def _native_coverage_from_report(report: dict[str, Any]) -> list[dict[str, Any]]:
    native = report.get("lanes", {}).get("native")
    if not isinstance(native, dict):
        return []
    coverage = native.get("coverage_chain")
    if isinstance(coverage, list):
        return coverage
    output_digest = native.get("output_digest")
    if not isinstance(output_digest, str):
        return []
    return [
        {
            "review_id": report["review_id"],
            "base_sha": report["base_sha"],
            "head_sha": report["head_sha"],
            "output_digest": output_digest,
        }
    ]


def _load_remediation_manifest(
    root: Path,
    *,
    change_id: str,
    prior_review: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    relative = (
        f".dls/cache/context/{change_id}/"
        f"remediation-{prior_review['review_id']}.json"
    )
    manifest = read_json(safe_resolve(root, relative))
    if manifest.get("schema_version") != 2:
        raise IntegrityError("Remediation manifest schema mismatch")
    digest = _remediation_manifest_digest(manifest)
    if manifest.get("manifest_digest") != digest:
        raise IntegrityError("Remediation manifest digest mismatch")
    expected = {
        "change_id": change_id,
        "review_id": prior_review["review_id"],
        "review_result_path": prior_review["result_path"],
        "review_result_digest": prior_review["result_digest"],
        "reviewed_head_sha": prior_review["head_sha"],
        "definition_digest": prior_review["definition_digest"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise IntegrityError(f"Remediation manifest is stale: {key}")
    return relative, manifest


def _remediation_manifest_digest(manifest: dict[str, Any]) -> str:
    digest_basis = {
        key: value for key, value in manifest.items() if key != "manifest_digest"
    }
    return sha256_bytes(
        json.dumps(
            digest_basis,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _disposition_applies_to_head(
    root: Path,
    disposition: dict[str, Any],
    head_sha: str,
) -> bool:
    disposition_sha = disposition.get("git_sha")
    if not isinstance(disposition_sha, str) or not disposition_sha:
        return False
    return (
        run_git(
            root,
            "merge-base",
            "--is-ancestor",
            disposition_sha,
            head_sha,
            check=False,
        ).returncode
        == 0
    )


def review_pack(
    root: Path,
    *,
    change_id: str,
    base_ref: str,
    head_ref: str | None,
    expected_revision: int,
    advisory_dirty: bool,
    operation_id: str | None,
    dry_run: bool = False,
    _review_mode: str = "full",
    _comparison_ref: str | None = None,
    _prior_review: dict[str, Any] | None = None,
    _prior_report: dict[str, Any] | None = None,
    _remediation_manifest: tuple[str, dict[str, Any]] | None = None,
    _operation_kind: str = "review-pack",
) -> dict[str, Any]:
    if not is_git_repo(root):
        raise IntegrityError("Review pack requires Git")
    state_store = StateStore(root)
    state = state_store.load(change_id)
    if (
        _review_mode == "full"
        and _operation_kind == "review-pack"
        and _latest_review_result(state) is not None
    ):
        raise IntegrityError(
            "Repeat acceptance review must use dls review-ready so prior findings "
            "and the remediation delta remain bound"
        )
    effective_operation_id = operation_id or str(uuid.uuid4())
    operation_kind = _operation_kind
    review_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"dls:{change_id}:{operation_kind}:{effective_operation_id}",
        )
    )
    relative_path = f".dls/reviews/{change_id}/packs/{review_id}.json"
    existing_operation = _operation(state, effective_operation_id)
    if existing_operation:
        _require_operation_kind(existing_operation, operation_kind)
        pack = read_json(safe_resolve(root, relative_path, must_exist=True))
        return {
            "ok": True,
            "dry_run": False,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "review_id": review_id,
            "review_pack_path": relative_path,
            "review_pack": pack,
        }
    _require_revision(state, expected_revision)
    dirty = git_source_dirty_paths(root)
    if dirty and not advisory_dirty:
        raise IntegrityError(f"Acceptance-grade review requires clean product source: {', '.join(dirty)}")
    approvals = derived_approval_statuses(root, state)
    definition_approval = next(
        (
            item
            for item in reversed(approvals)
            if item.get("decision") == "definition" and item.get("status") == "current"
        ),
        None,
    )
    if not definition_approval:
        raise IntegrityError("Review pack requires a current definition approval")
    if not advisory_dirty:
        gate = check(root, change_id=change_id, gate="review")
        if not gate["ok"]:
            failed = [item["id"] for item in gate["checks"] if not item["ok"]]
            raise IntegrityError(f"Review gate failed: {', '.join(failed)}")
    head_sha = run_git(root, "rev-parse", head_ref or "HEAD").stdout.strip()
    base_sha = run_git(root, "rev-parse", base_ref).stdout.strip()
    comparison_sha = run_git(
        root,
        "rev-parse",
        _comparison_ref or base_ref,
    ).stdout.strip()
    merge_base = git_merge_base(root, comparison_sha, head_sha)
    epic_merge_base = git_merge_base(root, base_sha, head_sha)
    mode = "advisory-dirty" if dirty else "acceptance-grade"
    source_snapshot_digest = git_source_snapshot_digest(root)
    if not source_snapshot_digest:
        raise IntegrityError("Review pack requires a Git source snapshot")
    current_evidence = _current_successful_evidence_paths(root, state)
    remediation_link: dict[str, Any] | None = None
    if _remediation_manifest:
        remediation_path, remediation_manifest = _remediation_manifest
        remediation_link = {
            "manifest_path": remediation_path,
            "manifest_digest": remediation_manifest["manifest_digest"],
            "review_id": remediation_manifest["review_id"],
            "review_result_digest": remediation_manifest["review_result_digest"],
            "reviewed_head_sha": remediation_manifest["reviewed_head_sha"],
        }
    required_prior_findings = (
        _active_prior_findings(root, state, include_waived=True)
        if _review_mode == "remediation"
        else []
    )
    prior_native_coverage = (
        _native_coverage_from_report(_prior_report)
        if _prior_report is not None
        else []
    )
    pack = {
        "schema_version": REVIEW_PACK_SCHEMA_VERSION,
        "review_id": review_id,
        "change_id": change_id,
        "mode": mode,
        "review_mode": _review_mode,
        "control_level": state["control_level"],
        "created_at": utc_now(),
        "base_sha": base_sha,
        "epic_base_sha": base_sha,
        "comparison_base_sha": comparison_sha,
        "head_sha": head_sha,
        "merge_base": merge_base,
        "epic_merge_base": epic_merge_base,
        "definition_digest": definition_approval["object_digest"],
        "source_snapshot_digest": source_snapshot_digest,
        "required_lanes": _review_pack_required_lanes(
            control_level=state["control_level"],
            mode=mode,
        ),
        "previous_pack": _previous_pack_link(root, state),
        "prior_review": _prior_review,
        "remediation_manifest": remediation_link,
        "risk_lenses": _risk_lenses(state) if _review_mode == "full" else [],
        "required_prior_findings": required_prior_findings,
        "prior_native_coverage": prior_native_coverage,
        "state_revision": state["state_revision"],
        "artifacts": state["artifacts"],
        "tickets": state["tickets"],
        "evidence": current_evidence,
        "finding_dispositions": state["finding_dispositions"],
        "changed_files": git_changed_files(root, comparison_sha, head_sha),
        "full_changed_files": git_changed_files(root, base_sha, head_sha),
        "source_dirty_paths": dirty,
    }
    pack["pack_digest"] = _review_pack_digest(pack)
    _validate_review_pack(pack, change_id)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "review_id": review_id,
            "review_pack_path": None,
            "review_pack": pack,
        }
    write_immutable_json(safe_resolve(root, relative_path), pack)

    def mutate(value: dict[str, Any]) -> None:
        value["reviews"].append(
            {
                "review_id": review_id,
                "kind": "pack",
                "pack_path": relative_path,
                "base_sha": base_sha,
                "comparison_base_sha": comparison_sha,
                "head_sha": head_sha,
                "mode": pack["mode"],
                "review_mode": pack["review_mode"],
                "pack_digest": pack["pack_digest"],
                "definition_digest": pack["definition_digest"],
                "source_snapshot_digest": pack["source_snapshot_digest"],
                "required_lanes": pack["required_lanes"],
                "previous_pack": pack["previous_pack"],
                "prior_review": pack["prior_review"],
                "remediation_manifest": pack["remediation_manifest"],
                "created_at": pack["created_at"],
            }
        )
        value["phase"] = "review"

    updated, changed = state_store.mutate(
        change_id,
        expected_revision=expected_revision,
        operation_id=effective_operation_id,
        operation_kind=operation_kind,
        mutator=mutate,
    )
    return {
        "ok": True,
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "operation_id": effective_operation_id,
        "review_id": review_id,
        "review_pack_path": relative_path,
        "review_pack": pack,
    }


def _review_ready_blocked(
    *,
    change_id: str,
    state_revision: int,
    next_action: str,
    detail: str,
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "ok": False,
        "dry_run": dry_run,
        "changed": False,
        "change_id": change_id,
        "state_revision": state_revision,
        "next_action": {
            "id": next_action,
            "detail": detail,
        },
        "review_pack_path": None,
        "review_pack": None,
    }


def review_ready(
    root: Path,
    *,
    change_id: str,
    base_ref: str,
    expected_revision: int,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not is_git_repo(root):
        raise IntegrityError("Review readiness requires Git")
    state = StateStore(root).load(change_id)
    _require_revision(state, expected_revision)
    dirty = git_source_dirty_paths(root)
    if dirty:
        return _review_ready_blocked(
            change_id=change_id,
            state_revision=state["state_revision"],
            next_action="commit-product-source",
            detail="dirty=" + ",".join(dirty),
            dry_run=dry_run,
        )
    approvals = derived_approval_statuses(root, state)
    definition_approval = next(
        (
            item
            for item in reversed(approvals)
            if item.get("decision") == "definition"
            and item.get("status") == "current"
        ),
        None,
    )
    if state["control_level"] in {"standard", "critical"} and not definition_approval:
        return _review_ready_blocked(
            change_id=change_id,
            state_revision=state["state_revision"],
            next_action="approve-definition",
            detail=f"definition_digest={current_definition_digest(root, state)}",
            dry_run=dry_run,
        )
    prior_review, prior_report = _prior_review_link(root, state)
    review_mode = "full"
    comparison_ref = base_ref
    remediation_manifest: tuple[str, dict[str, Any]] | None = None
    current_head = git_head(root)
    if prior_review is not None and prior_report is not None:
        review_mode = "remediation"
        comparison_ref = prior_review["head_sha"]
        manifest_path = (
            root
            / ".dls"
            / "cache"
            / "context"
            / change_id
            / f"remediation-{prior_review['review_id']}.json"
        )
        if not manifest_path.is_file():
            action = (
                "run-remediation-start"
                if current_head == prior_review["head_sha"]
                else "restore-reviewed-head-and-run-remediation-start"
            )
            return _review_ready_blocked(
                change_id=change_id,
                state_revision=state["state_revision"],
                next_action=action,
                detail=(
                    f"missing={manifest_path.relative_to(root)}; "
                    f"reviewed_head={prior_review['head_sha']}"
                ),
                dry_run=dry_run,
            )
        remediation_manifest = _load_remediation_manifest(
            root,
            change_id=change_id,
            prior_review=prior_review,
        )
        if current_head == prior_review["head_sha"]:
            return _review_ready_blocked(
                change_id=change_id,
                state_revision=state["state_revision"],
                next_action="commit-remediation",
                detail="candidate HEAD still equals the previous reviewed HEAD",
                dry_run=dry_run,
            )
    incomplete_tickets = sorted(
        ticket_id
        for ticket_id, ticket in state["tickets"].items()
        if ticket.get("status") not in {"implemented", "validated", "done"}
    )
    if incomplete_tickets:
        return _review_ready_blocked(
            change_id=change_id,
            state_revision=state["state_revision"],
            next_action="implement-tickets",
            detail="incomplete=" + ",".join(incomplete_tickets),
            dry_run=dry_run,
        )
    evidence_ok, evidence_detail, _ = _required_evidence_status(
        root,
        state,
        stage="review",
    )
    if not evidence_ok:
        return _review_ready_blocked(
            change_id=change_id,
            state_revision=state["state_revision"],
            next_action="run-review-validation",
            detail=evidence_detail,
            dry_run=dry_run,
        )
    if prior_review is not None and prior_report is not None:
        unaddressed: list[str] = []
        for finding in _active_prior_findings(
            root,
            state,
            include_waived=True,
        ):
            disposition = finding.get("disposition")
            if not isinstance(disposition, dict):
                unaddressed.append(finding["finding_id"])
                continue
            status = disposition.get("status")
            if status == "waived":
                continue
            if status != "addressed" or not _disposition_applies_to_head(
                root,
                disposition,
                current_head or "",
            ):
                unaddressed.append(finding["finding_id"])
        if unaddressed:
            return _review_ready_blocked(
                change_id=change_id,
                state_revision=state["state_revision"],
                next_action="address-review-findings",
                detail="unaddressed=" + ",".join(sorted(unaddressed)),
                dry_run=dry_run,
            )
    result = review_pack(
        root,
        change_id=change_id,
        base_ref=base_ref,
        head_ref=None,
        expected_revision=expected_revision,
        advisory_dirty=False,
        operation_id=operation_id,
        dry_run=dry_run,
        _review_mode=review_mode,
        _comparison_ref=comparison_ref,
        _prior_review=prior_review,
        _prior_report=prior_report,
        _remediation_manifest=remediation_manifest,
        _operation_kind="review-ready",
    )
    result["next_action"] = {
        "id": "start-review",
        "detail": result.get("review_pack_path") or "dry-run pack is ready",
    }
    return result


def _review_pack_owner(path: Path) -> Path:
    for candidate in (path.parent, *path.parent.parents):
        if (candidate / ".dls" / "config.toml").is_file():
            return candidate
    raise IntegrityError(
        f"Absolute ReviewPack is not inside an initialized DLS checkout: {path}"
    )


def _resolve_review_pack(
    root: Path,
    *,
    change_id: str,
    pack_path: str | None,
) -> tuple[Path, dict[str, Any], dict[str, Any], str, str]:
    root = root.resolve()
    owner_selection = "current-checkout"
    if pack_path is not None:
        requested = Path(pack_path)
        if requested.is_absolute():
            candidate = requested.resolve()
            owner = _review_pack_owner(candidate)
            owner_selection = "absolute-pack"
        else:
            owner = root
            candidate = safe_resolve(owner, requested, must_exist=True)
    else:
        owner = root
        local_state_path = StateStore(owner).path(change_id)
        if not (
            (owner / ".dls" / "config.toml").is_file()
            and local_state_path.is_file()
        ):
            owner = resolve_registered_worktree(root, change_id)
            owner_selection = "registered-worktree"
        state = StateStore(owner).load(change_id)
        completed_review_ids = {
            entry.get("review_id")
            for entry in state["reviews"]
            if entry.get("kind") == "result"
        }
        pack_entry = next(
            (
                entry
                for entry in reversed(state["reviews"])
                if entry.get("kind") == "pack"
                and entry.get("review_id") not in completed_review_ids
                and isinstance(entry.get("pack_path"), str)
            ),
            None,
        )
        if not pack_entry:
            raise IntegrityError(
                f"No unfinished ReviewPack for {change_id} in {owner}; "
                "DLS will not infer a branch or scan neighboring worktrees"
            )
        candidate = safe_resolve(owner, pack_entry["pack_path"], must_exist=True)
    if not (owner / ".dls" / "config.toml").is_file():
        raise IntegrityError(f"ReviewPack owner is not initialized for DLS: {owner}")
    try:
        relative_path = str(candidate.relative_to(owner))
    except ValueError as exc:
        raise IntegrityError(f"ReviewPack escapes its owner checkout: {candidate}") from exc
    expected_parent = Path(".dls") / "reviews" / change_id / "packs"
    if Path(relative_path).parent != expected_parent:
        raise IntegrityError(
            f"ReviewPack must be under {expected_parent}, got {relative_path}"
        )
    state = StateStore(owner).load(change_id)
    pack = read_json(candidate)
    _validate_review_pack(pack, change_id)
    pack_entry = next(
        (
            entry
            for entry in state["reviews"]
            if entry.get("kind") == "pack"
            and entry.get("review_id") == pack["review_id"]
            and entry.get("pack_path") == relative_path
        ),
        None,
    )
    if not pack_entry:
        raise IntegrityError("ReviewPack is not registered in its owner checkout state")
    if pack_entry.get("pack_digest") != pack["pack_digest"]:
        raise IntegrityError("ReviewPack digest does not match DLS state")
    return owner, state, pack, relative_path, owner_selection


def _validate_review_pack_current(
    root: Path,
    *,
    state: dict[str, Any],
    pack: dict[str, Any],
) -> None:
    if state["control_level"] != pack["control_level"]:
        raise IntegrityError("ReviewPack control level no longer matches DLS state")
    current_head = git_head(root)
    if current_head != pack["head_sha"]:
        raise IntegrityError(
            f"ReviewPack HEAD is not current checkout HEAD: {pack['head_sha']} != {current_head}"
        )
    current_snapshot = git_source_snapshot_digest(root)
    if current_snapshot != pack["source_snapshot_digest"]:
        raise IntegrityError("Product source changed after ReviewPack creation")
    if pack["mode"] == "acceptance-grade":
        dirty = git_source_dirty_paths(root)
        if dirty:
            raise IntegrityError(
                "Acceptance-grade ReviewPack requires clean product source: "
                + ", ".join(dirty)
            )
    current_definition = current_definition_digest(root, state)
    if current_definition != pack["definition_digest"]:
        raise IntegrityError("ReviewPack definition digest is stale")
    current_approval = next(
        (
            item
            for item in reversed(derived_approval_statuses(root, state))
            if item.get("decision") == "definition"
            and item.get("status") == "current"
            and item.get("object_digest") == pack["definition_digest"]
        ),
        None,
    )
    if not current_approval:
        raise IntegrityError("ReviewPack definition approval is no longer current")
    if run_git(root, "rev-parse", pack["base_sha"]).stdout.strip() != pack["base_sha"]:
        raise IntegrityError("ReviewPack base SHA cannot be resolved exactly")
    if run_git(root, "rev-parse", pack["head_sha"]).stdout.strip() != pack["head_sha"]:
        raise IntegrityError("ReviewPack head SHA cannot be resolved exactly")
    comparison_base = pack.get("comparison_base_sha", pack["base_sha"])
    if git_merge_base(root, comparison_base, pack["head_sha"]) != pack["merge_base"]:
        raise IntegrityError("ReviewPack merge-base changed")
    if git_changed_files(root, comparison_base, pack["head_sha"]) != pack["changed_files"]:
        raise IntegrityError("ReviewPack changed-file inventory changed")
    if pack.get("schema_version") == REVIEW_PACK_SCHEMA_VERSION:
        if (
            git_merge_base(root, pack["epic_base_sha"], pack["head_sha"])
            != pack["epic_merge_base"]
        ):
            raise IntegrityError("ReviewPack epic merge-base changed")
        if (
            git_changed_files(root, pack["epic_base_sha"], pack["head_sha"])
            != pack["full_changed_files"]
        ):
            raise IntegrityError("ReviewPack full changed-file inventory changed")
        prior_review = pack.get("prior_review")
        if isinstance(prior_review, dict):
            result_path = prior_review.get("result_path")
            if not isinstance(result_path, str):
                raise IntegrityError("ReviewPack prior result path is invalid")
            entry = next(
                (
                    item
                    for item in state["reviews"]
                    if item.get("kind") == "result"
                    and item.get("review_id") == prior_review.get("review_id")
                ),
                None,
            )
            if not entry or entry.get("result_digest") != prior_review.get("result_digest"):
                raise IntegrityError("ReviewPack prior result digest is stale")
            actual_path, actual_report = _read_review_result(root, entry)
            if (
                actual_path != result_path
                or actual_report.get("head_sha") != prior_review.get("head_sha")
                or actual_report.get("review_id") != prior_review.get("review_id")
            ):
                raise IntegrityError("ReviewPack prior result content is stale")
        remediation = pack.get("remediation_manifest")
        if isinstance(remediation, dict):
            manifest_path = remediation.get("manifest_path")
            if not isinstance(manifest_path, str):
                raise IntegrityError("ReviewPack remediation manifest path is invalid")
            manifest = read_json(safe_resolve(root, manifest_path, must_exist=True))
            if (
                manifest.get("manifest_digest") != remediation.get("manifest_digest")
                or manifest.get("manifest_digest")
                != _remediation_manifest_digest(manifest)
            ):
                raise IntegrityError("ReviewPack remediation manifest digest is stale")
    for evidence_path in pack["evidence"]:
        record = read_json(safe_resolve(root, evidence_path, must_exist=True))
        if record.get("change_id") != pack["change_id"]:
            raise IntegrityError(f"Evidence belongs to another change: {evidence_path}")
        if (
            pack.get("schema_version") == REVIEW_PACK_SCHEMA_VERSION
            and (
                record.get("git_sha") != pack["head_sha"]
                or record.get("source_digest") != pack["source_snapshot_digest"]
                or record.get("exit_code") != 0
            )
        ):
            raise IntegrityError(f"Evidence is not current and successful: {evidence_path}")


def _native_review_argv(
    pack: dict[str, Any],
    final_output_path: str,
) -> list[str]:
    return [
        "codex",
        "exec",
        "review",
        "--strict-config",
        "-c",
        f'model="{NATIVE_REVIEW_MODEL}"',
        "-c",
        f'model_reasoning_effort="{NATIVE_REVIEW_REASONING_EFFORT}"',
        "-c",
        'sandbox_mode="read-only"',
        "--ephemeral",
        "--output-last-message",
        final_output_path,
        "--base",
        pack.get("comparison_base_sha", pack["merge_base"]),
    ]


def _successful_native_entry(
    root: Path,
    *,
    state: dict[str, Any],
    review_id: str,
) -> dict[str, Any] | None:
    entry = next(
        (
            item
            for item in reversed(state["reviews"])
            if item.get("kind") == "native"
            and item.get("review_id") == review_id
            and item.get("status") == "completed"
        ),
        None,
    )
    if not entry:
        return None
    output_path = entry.get("output_path")
    if not isinstance(output_path, str):
        raise IntegrityError("Native review metadata is missing output_path")
    output = safe_resolve(root, output_path)
    if not output.is_file():
        raise IntegrityError(f"Native review cache is missing: {output_path}")
    if sha256_file(output) != entry.get("output_digest"):
        raise IntegrityError("Native review cache digest mismatch")
    transcript_path = entry.get("transcript_path")
    if isinstance(transcript_path, str):
        transcript = safe_resolve(root, transcript_path)
        if not transcript.is_file():
            raise IntegrityError(
                f"Native review diagnostic transcript is missing: {transcript_path}"
            )
        if sha256_file(transcript) != entry.get("transcript_digest"):
            raise IntegrityError(
                "Native review diagnostic transcript digest mismatch"
            )
    return entry


def _semantic_review_effort(state: dict[str, Any]) -> str:
    high_risk = {"concurrency", "security-privacy", "auth"}
    if state["control_level"] == "critical" and high_risk.intersection(
        state["impact_tags"]
    ):
        return "xhigh"
    return "high"


def review_start(
    root: Path,
    *,
    change_id: str,
    pack_path: str | None,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    owner, state, pack, relative_pack_path, owner_selection = _resolve_review_pack(
        root,
        change_id=change_id,
        pack_path=pack_path,
    )
    _validate_review_pack_current(owner, state=state, pack=pack)
    effective_operation_id = operation_id or str(uuid.uuid4())
    attempt_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"dls:{change_id}:native-review:{effective_operation_id}",
        )
    )
    relative_output_path = (
        f".dls/cache/reviews/{change_id}/{pack['review_id']}/"
        f"native-final-{attempt_id}.txt"
    )
    relative_transcript_path = (
        f".dls/cache/reviews/{change_id}/{pack['review_id']}/"
        f"native-transcript-{attempt_id}.txt"
    )
    native_required = "native-diff" in pack["required_lanes"]
    native_entry = _successful_native_entry(
        owner,
        state=state,
        review_id=pack["review_id"],
    )
    existing_operation = _operation(state, effective_operation_id)
    if existing_operation and native_entry is None:
        _require_operation_kind(existing_operation, "review-start")
        prior_attempt = next(
            (
                item
                for item in reversed(state["reviews"])
                if item.get("kind") == "native"
                and item.get("review_id") == pack["review_id"]
                and item.get("operation_id") == effective_operation_id
            ),
            None,
        )
        if prior_attempt:
            raise IntegrityError(
                "Native review operation already finished without success: "
                f"status={prior_attempt.get('status')}"
            )
        raise IntegrityError(
            f"review-start operation has no matching native attempt: {effective_operation_id}"
        )
    argv = _native_review_argv(pack, relative_output_path)
    if native_entry and isinstance(native_entry.get("argv"), list):
        argv = native_entry["argv"]
    if dry_run:
        context = build_context(
            owner,
            change_id=change_id,
            phase="review",
            include=[],
            exclude=[],
            dry_run=True,
        )
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "owner_root": str(owner),
            "owner_selection": owner_selection,
            "review_id": pack["review_id"],
            "review_pack_path": relative_pack_path,
            "review_mode": pack.get("review_mode", "full"),
            "risk_lenses": pack.get("risk_lenses", []),
            "required_prior_findings": pack.get("required_prior_findings", []),
            "native_required": native_required,
            "native_reused": native_entry is not None,
            "native_argv": argv if native_required else None,
            "native": native_entry,
            "review_context_path": None,
            "review_context_digest": context["manifest"]["manifest_digest"],
            "semantic_model": "gpt-5.6-sol",
            "semantic_reasoning_effort": _semantic_review_effort(state),
        }
    changed = False
    if native_required and native_entry is None:
        output_path = safe_resolve(owner, relative_output_path)
        transcript_path = safe_resolve(owner, relative_transcript_path)
        if output_path.exists() or transcript_path.exists():
            raise IntegrityError(
                "Native review cache already exists without matching state; "
                "retry with a new operation ID"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_before = git_source_snapshot_digest(owner)
        execution = _run_bounded_command(
            argv,
            cwd=owner,
            environment=allowed_environment([]),
            timeout_seconds=NATIVE_REVIEW_TIMEOUT_SECONDS,
            max_output_bytes=NATIVE_REVIEW_TRANSCRIPT_MAX_BYTES,
            terminate_on_overflow=False,
        )
        transcript_text = execution["output"].decode("utf-8", errors="replace")
        atomic_write_text(transcript_path, transcript_text, backup=False)
        output_exists = output_path.is_file()
        output_bytes = output_path.stat().st_size if output_exists else 0
        output_overflow = output_bytes > NATIVE_REVIEW_MAX_OUTPUT_BYTES
        if output_overflow:
            with output_path.open("rb") as handle:
                retained_final = handle.read(NATIVE_REVIEW_MAX_OUTPUT_BYTES)
            atomic_write_text(
                output_path,
                retained_final.decode("utf-8", errors="replace"),
                backup=False,
            )
        output_digest = sha256_file(output_path) if output_exists else None
        snapshot_after = git_source_snapshot_digest(owner)
        status_value = "completed"
        if execution["timed_out"]:
            status_value = "timeout"
        elif execution["exit_code"] != 0:
            status_value = "failed"
        elif not output_exists or output_bytes == 0:
            status_value = "missing-output"
        elif output_overflow:
            status_value = "output-cap"
        elif snapshot_after != snapshot_before:
            status_value = "source-changed"
        native_entry = {
            "review_id": pack["review_id"],
            "kind": "native",
            "attempt_id": attempt_id,
            "operation_id": effective_operation_id,
            "status": status_value,
            "base_sha": pack.get("comparison_base_sha", pack["base_sha"]),
            "head_sha": pack["head_sha"],
            "pack_digest": pack["pack_digest"],
            "model": NATIVE_REVIEW_MODEL,
            "reasoning_effort": NATIVE_REVIEW_REASONING_EFFORT,
            "argv": argv,
            "output_path": relative_output_path if output_exists else None,
            "output_digest": output_digest,
            "output_bytes": output_bytes,
            "exit_code": execution["exit_code"],
            "timed_out": execution["timed_out"],
            "overflow": output_overflow,
            "transcript_path": relative_transcript_path,
            "transcript_digest": sha256_file(transcript_path),
            "transcript_output_bytes": execution["output_bytes"],
            "transcript_retained_bytes": len(execution["output"]),
            "transcript_truncated": execution["overflow"],
            "duration_seconds": execution["duration_seconds"],
            "source_snapshot_digest": snapshot_after,
            "completed_at": utc_now(),
        }

        def mutate(value: dict[str, Any]) -> None:
            value["reviews"].append(native_entry)

        try:
            updated, changed = StateStore(owner).mutate(
                change_id,
                expected_revision=state["state_revision"],
                operation_id=effective_operation_id,
                operation_kind="review-start",
                mutator=mutate,
            )
        except Exception:
            output_path.unlink(missing_ok=True)
            transcript_path.unlink(missing_ok=True)
            raise
        state = updated
        if status_value != "completed":
            raise IntegrityError(
                f"Native review did not complete: status={status_value}; "
                f"transcript={relative_transcript_path}"
            )
        _validate_review_pack_current(owner, state=state, pack=pack)
    context = build_context(
        owner,
        change_id=change_id,
        phase="review",
        include=[],
        exclude=[],
        dry_run=False,
    )
    native_coverage = list(pack.get("prior_native_coverage", []))
    if native_entry:
        native_coverage.append(
            {
                "review_id": pack["review_id"],
                "base_sha": pack.get("comparison_base_sha", pack["base_sha"]),
                "head_sha": pack["head_sha"],
                "output_digest": native_entry["output_digest"],
            }
        )
    return {
        "ok": True,
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "operation_id": effective_operation_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "review_id": pack["review_id"],
        "review_pack_path": relative_pack_path,
        "review_mode": pack.get("review_mode", "full"),
        "risk_lenses": pack.get("risk_lenses", []),
        "required_prior_findings": pack.get("required_prior_findings", []),
        "native_required": native_required,
        "native_reused": native_required and not changed,
        "native_argv": argv if native_required else None,
        "native": native_entry,
        "native_coverage": native_coverage,
        "review_context_path": context["manifest_path"],
        "review_context_digest": context["manifest"]["manifest_digest"],
        "semantic_model": "gpt-5.6-sol",
        "semantic_reasoning_effort": _semantic_review_effort(state),
    }


def review_import(
    root: Path,
    *,
    change_id: str,
    report_path: str,
    expected_revision: int,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_store = StateStore(root)
    state = state_store.load(change_id)
    effective_operation_id = operation_id or str(uuid.uuid4())
    operation_kind = "review-import"
    existing_operation = _operation(state, effective_operation_id)
    if existing_operation:
        _require_operation_kind(existing_operation, operation_kind)
    source_path = safe_resolve(root, report_path, must_exist=True)
    report = read_json(source_path)
    if not isinstance(report.get("review_id"), str) or not report["review_id"]:
        raise IntegrityError("Review report missing review_id")
    review_id = report["review_id"]
    relative_path = f".dls/reviews/{change_id}/results/{review_id}.json"
    pack_entry = next(
        (
            entry
            for entry in state["reviews"]
            if entry.get("review_id") == review_id and entry.get("kind") == "pack"
        ),
        None,
    )
    if not pack_entry:
        raise IntegrityError(f"No review pack for review_id: {review_id}")
    pack_path = pack_entry.get("pack_path")
    if not isinstance(pack_path, str):
        raise IntegrityError("ReviewPack state entry is missing pack_path")
    pack = read_json(safe_resolve(root, pack_path, must_exist=True))
    _validate_review_pack(pack, change_id)
    if pack_entry.get("pack_digest") != pack["pack_digest"]:
        raise IntegrityError("ReviewPack digest does not match DLS state")
    _validate_review_pack_current(root, state=state, pack=pack)
    _validate_review_report(report, change_id, pack)
    if report["base_sha"] != pack_entry["base_sha"] or report["head_sha"] != pack_entry["head_sha"]:
        raise IntegrityError("Review report base/head does not match its pack")
    if report["pack_digest"] != pack["pack_digest"]:
        raise IntegrityError("Review report pack digest mismatch")
    if report["definition_digest"] != pack["definition_digest"]:
        raise IntegrityError("Review report definition digest mismatch")
    native_entry = _successful_native_entry(
        root,
        state=state,
        review_id=review_id,
    )
    native_required = "native-diff" in pack["required_lanes"]
    if native_required and not native_entry:
        raise IntegrityError(
            "Acceptance-grade standard/critical review requires a successful native pass"
        )
    native_lane = report["lanes"].get("native")
    if native_required:
        if not isinstance(native_lane, dict):
            raise IntegrityError("ReviewIR is missing required native lane provenance")
        for report_key, state_key in (
            ("attempt_id", "attempt_id"),
            ("model", "model"),
            ("reasoning_effort", "reasoning_effort"),
            ("output_path", "output_path"),
            ("output_digest", "output_digest"),
            ("source_snapshot_digest", "source_snapshot_digest"),
        ):
            if native_lane.get(report_key) != native_entry.get(state_key):
                raise IntegrityError(
                    f"ReviewIR native provenance mismatch: {report_key}"
                )
        if report.get("schema_version") == REVIEW_IR_SCHEMA_VERSION:
            expected_coverage = list(pack["prior_native_coverage"])
            expected_coverage.append(
                {
                    "review_id": review_id,
                    "base_sha": pack["comparison_base_sha"],
                    "head_sha": pack["head_sha"],
                    "output_digest": native_entry["output_digest"],
                }
            )
            if native_lane.get("coverage_chain") != expected_coverage:
                raise IntegrityError("ReviewIR native coverage chain is not continuous")
            if expected_coverage:
                if expected_coverage[0]["base_sha"] != pack["epic_base_sha"]:
                    raise IntegrityError(
                        "ReviewIR native coverage does not start at epic base"
                    )
                for previous, current in zip(
                    expected_coverage,
                    expected_coverage[1:],
                ):
                    if previous["head_sha"] != current["base_sha"]:
                        raise IntegrityError(
                            "ReviewIR native coverage chain has a gap"
                        )
                if expected_coverage[-1]["head_sha"] != pack["head_sha"]:
                    raise IntegrityError(
                        "ReviewIR native coverage does not reach current HEAD"
                    )
    elif native_lane is not None:
        raise IntegrityError("ReviewIR declares a native lane that its ReviewPack did not require")
    semantic_lane = report["lanes"]["semantic"]
    if semantic_lane["reasoning_effort"] != _semantic_review_effort(state):
        raise IntegrityError("ReviewIR semantic reasoning effort does not match change risk")
    context_relative = Path(semantic_lane["context_manifest_path"])
    expected_context_parent = Path(".dls") / "cache" / "context" / change_id
    if context_relative.parent != expected_context_parent:
        raise IntegrityError("ReviewIR semantic context path is outside DLS review cache")
    context_path = safe_resolve(
        root,
        semantic_lane["context_manifest_path"],
        must_exist=True,
    )
    context_manifest = read_json(context_path)
    if (
        context_manifest.get("phase") != "review"
        or context_manifest.get("change_id") != change_id
        or context_manifest.get("git_head") != pack["head_sha"]
        or context_manifest.get("manifest_digest")
        != semantic_lane["context_manifest_digest"]
        or context_manifest.get("manifest_digest")
        != _context_manifest_content_digest(context_manifest)
    ):
        raise IntegrityError("ReviewIR semantic context provenance mismatch")
    if not any(
        item.get("path") == pack_path
        and item.get("sha256")
        == sha256_file(safe_resolve(root, pack_path, must_exist=True))
        for item in context_manifest.get("inputs", [])
        if isinstance(item, dict)
    ):
        raise IntegrityError("Semantic review context does not contain its ReviewPack")
    draft_relative = Path(semantic_lane["independent_draft_path"])
    expected_draft_parent = (
        Path(".dls") / "cache" / "reviews" / change_id / review_id
    )
    if draft_relative.parent != expected_draft_parent:
        raise IntegrityError("Independent semantic draft is outside its review cache")
    draft_path = safe_resolve(
        root,
        semantic_lane["independent_draft_path"],
        must_exist=True,
    )
    if sha256_file(draft_path) != semantic_lane["independent_draft_digest"]:
        raise IntegrityError("Independent semantic draft digest mismatch")
    if report.get("schema_version") == REVIEW_IR_SCHEMA_VERSION:
        for item in semantic_lane["passes"]:
            relative = Path(item["draft_path"])
            if relative.parent != expected_draft_parent:
                raise IntegrityError("Semantic pass draft is outside its review cache")
            path = safe_resolve(root, item["draft_path"], must_exist=True)
            if sha256_file(path) != item["draft_digest"]:
                raise IntegrityError(
                    f"Semantic pass draft digest mismatch: {item['kind']}"
                )
        for item in report["lanes"].get("specialists", []):
            relative = Path(item["draft_path"])
            if relative.parent != expected_draft_parent:
                raise IntegrityError("Specialist draft is outside its review cache")
            path = safe_resolve(root, item["draft_path"], must_exist=True)
            if sha256_file(path) != item["draft_digest"]:
                raise IntegrityError(
                    f"Specialist draft digest mismatch: {item['lens_id']}"
                )
    if git_source_snapshot_digest(root) != pack["source_snapshot_digest"]:
        raise IntegrityError("Product source changed during semantic review")
    existing_result = next(
        (
            entry
            for entry in state["reviews"]
            if entry.get("review_id") == review_id and entry.get("kind") == "result"
        ),
        None,
    )
    if existing_result:
        recorded = read_json(safe_resolve(root, relative_path, must_exist=True))
        if recorded != report:
            raise IntegrityError(f"Review result already imported with different content: {review_id}")
        return {
            "ok": report["verdict"] == "review-clear",
            "dry_run": False,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "review_result_path": relative_path,
            "verdict": report["verdict"],
            "finding_counts": _finding_counts(report["findings"]),
        }
    if existing_operation:
        raise IntegrityError(f"Review import operation has no matching result: {effective_operation_id}")
    _require_revision(state, expected_revision)
    if dry_run:
        return {
            "ok": report["verdict"] == "review-clear",
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "review_result_path": None,
            "verdict": report["verdict"],
            "finding_counts": _finding_counts(report["findings"]),
        }
    result_digest = sha256_bytes(
        json.dumps(
            report,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )

    def mutate(value: dict[str, Any]) -> None:
        if any(
            entry.get("review_id") == review_id and entry.get("kind") == "result"
            for entry in value["reviews"]
        ):
            return
        value["reviews"].append(
            {
                "review_id": review_id,
                "kind": "result",
                "result_path": relative_path,
                "base_sha": report["base_sha"],
                "comparison_base_sha": report.get(
                    "comparison_base_sha",
                    report["base_sha"],
                ),
                "head_sha": report["head_sha"],
                "mode": pack_entry["mode"],
                "review_mode": report.get("review_mode", "full"),
                "verdict": report["verdict"],
                "pack_digest": pack["pack_digest"],
                "definition_digest": pack["definition_digest"],
                "source_snapshot_digest": pack["source_snapshot_digest"],
                "result_digest": result_digest,
                "finding_counts": _finding_counts(report["findings"]),
                "imported_at": utc_now(),
            }
        )
        if report.get("schema_version") == REVIEW_IR_SCHEMA_VERSION:
            for prior in report["prior_finding_verdicts"]:
                if prior["verdict"] != "verified":
                    continue
                disposition_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"dls:{change_id}:verified:{review_id}:{prior['finding_id']}",
                    )
                )
                value["finding_dispositions"].append(
                    {
                        "id": disposition_id,
                        "finding_id": prior["finding_id"],
                        "status": "verified",
                        "rationale": "Verified by independent repeat review.",
                        "git_sha": report["head_sha"],
                        "evidence": prior["evidence"],
                        "actor": "reviewer",
                        "authority": "independent-review",
                        "review_id": review_id,
                        "recorded_at": utc_now(),
                    }
                )
        value["lifecycle"] = report["verdict"]

    updated, changed = state_store.mutate_with_immutable_artifact(
        change_id,
        expected_revision=expected_revision,
        operation_id=effective_operation_id,
        operation_kind=operation_kind,
        artifact_path=safe_resolve(root, relative_path),
        artifact_value=report,
        mutator=mutate,
    )
    return {
        "ok": report["verdict"] == "review-clear",
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "operation_id": effective_operation_id,
        "review_result_path": relative_path,
        "verdict": report["verdict"],
        "finding_counts": _finding_counts(report["findings"]),
    }


def finding_disposition(
    root: Path,
    *,
    change_id: str,
    finding_id: str,
    disposition_status: str,
    rationale: str,
    expected_revision: int,
    git_sha: str | None,
    evidence: list[str],
    actor: str,
    prompt: str | None,
    response: str | None,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    if disposition_status not in WRITABLE_DISPOSITION_STATUSES:
        raise UsageError(f"Invalid finding disposition: {disposition_status}")
    normalized_status = _normalized_disposition_status(disposition_status)
    if normalized_status is None:
        raise UsageError(f"Invalid finding disposition: {disposition_status}")
    if not rationale.strip():
        raise UsageError("Finding disposition requires a rationale")
    if actor not in {"codex", "user"}:
        raise UsageError("actor must be codex or user")
    if normalized_status in {"addressed", "waived"} and not evidence:
        raise IntegrityError(f"{normalized_status} disposition requires evidence")
    state_store = StateStore(root)
    state = state_store.load(change_id)
    effective_operation_id = operation_id or str(uuid.uuid4())
    operation_kind = f"finding:{finding_id}:{disposition_status}"
    disposition_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"dls:{change_id}:finding:{finding_id}:{effective_operation_id}",
        )
    )
    existing_operation = _operation(state, effective_operation_id)
    if existing_operation:
        _require_operation_kind(existing_operation, operation_kind)
        recorded = next(
            (
                item
                for item in state["finding_dispositions"]
                if item.get("id") == disposition_id
            ),
            None,
        )
        if not recorded:
            raise IntegrityError(
                f"Finding operation has no matching disposition: {effective_operation_id}"
            )
        return {
            "ok": True,
            "dry_run": False,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "disposition": recorded,
        }
    _require_revision(state, expected_revision)
    latest_result = _latest_review_result(state)
    if not latest_result:
        raise IntegrityError("Finding disposition requires an imported review result")
    if finding_id not in _all_review_findings(root, state):
        raise IntegrityError(f"Finding is not present in imported ReviewIR history: {finding_id}")
    current_head = git_head(root)
    effective_sha = git_sha or current_head
    if git_sha and current_head and git_sha != current_head:
        raise IntegrityError(f"Disposition SHA is not current HEAD: {git_sha} != {current_head}")
    binding_digest = effective_sha or current_definition_digest(root, state)
    if normalized_status == "waived" and actor == "codex":
        _validate_scoped_confirmation(f"waive {finding_id}", binding_digest, prompt, response)
    for relative in evidence:
        safe_resolve(root, relative, must_exist=True)
    record = {
        "id": disposition_id,
        "finding_id": finding_id,
        "status": normalized_status,
        "rationale": rationale,
        "git_sha": effective_sha,
        "evidence": evidence,
        "actor": actor,
        "authority": "user" if normalized_status == "waived" else "workflow",
        "prompt": prompt,
        "response": response,
        "recorded_at": utc_now(),
    }
    if disposition_status == "resolved":
        record["legacy_alias"] = "resolved"
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "disposition": record,
        }

    def mutate(value: dict[str, Any]) -> None:
        value["finding_dispositions"].append(record)

    updated, changed = state_store.mutate(
        change_id,
        expected_revision=expected_revision,
        operation_id=effective_operation_id,
        operation_kind=operation_kind,
        mutator=mutate,
    )
    recorded = next(
        (item for item in updated["finding_dispositions"] if item.get("id") == record["id"]),
        record,
    )
    return {
        "ok": True,
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "operation_id": effective_operation_id,
        "disposition": recorded,
    }


def doctor(root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    manifest_path = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_ok = manifest.get("name") == "dls" and manifest.get("version") == VERSION
    except (OSError, json.JSONDecodeError):
        manifest = {}
        manifest_ok = False
    checks.append(_check("plugin:manifest", manifest_ok, str(manifest_path)))
    schema_ok = True
    for schema_path in sorted(SCHEMAS_ROOT.glob("*.json")):
        try:
            json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            schema_ok = False
    checks.append(_check("schemas:json", schema_ok, str(SCHEMAS_ROOT)))
    profile_ok = True
    for profile_path in sorted(PROFILES_ROOT.glob("*.toml")):
        try:
            __import__("tomllib").loads(profile_path.read_text(encoding="utf-8"))
        except Exception:
            profile_ok = False
    checks.append(_check("profiles:toml", profile_ok, str(PROFILES_ROOT)))
    active_skills: list[str] = []
    skill_metadata_ok = True
    for skill_path in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")):
        text = skill_path.read_text(encoding="utf-8")
        yaml_path = skill_path.parent / "agents" / "openai.yaml"
        yaml_text = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else ""
        if (
            not text.startswith("---\n")
            or "description:" not in text.split("---", 2)[1]
            or "allow_implicit_invocation: false" not in yaml_text
        ):
            skill_metadata_ok = False
        active_skills.append(skill_path.parent.name)
    checks.append(
        _check(
            "skills:explicit-only",
            skill_metadata_ok and set(active_skills) == {"dls-workflow", "dls-debug"},
            ",".join(active_skills),
        )
    )
    python_ok = tuple(__import__("sys").version_info[:2]) >= (3, 11)
    checks.append(
        _check(
            "runtime:python",
            python_ok,
            __import__("platform").python_version(),
        )
    )
    checks.append(_check("git:repository", is_git_repo(root), str(root)))
    config_path = root / ".dls" / "config.toml"
    config_ok = False
    if config_path.is_file():
        try:
            load_config(root)
            config_ok = True
        except ConfigError:
            config_ok = False
    checks.append(_check("repository:config", config_ok, str(config_path)))
    source_digest = sha256_file(manifest_path) if manifest_path.is_file() else None
    installed_root = os.environ.get("DLS_PLUGIN_ROOT")
    runtime_match: bool | None = None
    runtime_digest: str | None = None
    if installed_root:
        runtime_manifest = Path(installed_root) / ".codex-plugin" / "plugin.json"
        if runtime_manifest.is_file():
            runtime_digest = sha256_file(runtime_manifest)
            runtime_match = runtime_digest == source_digest
        checks.append(
            _check(
                "runtime:source-match",
                runtime_match is True,
                str(runtime_manifest),
            )
        )
    conflicts = _global_conflict_inventory()
    return {
        "ok": all(item["ok"] for item in checks),
        "version": VERSION,
        "schema_version": SCHEMA_VERSION,
        "plugin_root": str(PLUGIN_ROOT),
        "plugin_manifest_sha256": source_digest,
        "runtime_plugin_root": installed_root,
        "runtime_manifest_sha256": runtime_digest,
        "runtime_matches_source": runtime_match,
        "active_skills": active_skills,
        "checks": checks,
        "global_conflicts": conflicts,
    }


def validate_command(
    root: Path,
    *,
    change_id: str,
    command_id: str,
    expected_revision: int,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    command = command_config(root, command_id)
    cwd = safe_resolve(root, command["cwd"], must_exist=True)
    state = StateStore(root).load(change_id)
    effective_operation_id = operation_id or str(uuid.uuid4())
    if _has_operation(state, effective_operation_id):
        result = evidence_add(
            root,
            change_id=change_id,
            command_id=command_id,
            exit_code=0,
            summary="idempotent retry",
            expected_revision=state["state_revision"],
            git_sha=git_head(root),
            artifacts=[],
            environment=None,
            duration_seconds=None,
            operation_id=effective_operation_id,
        )
        result["validation"] = {"idempotent_retry": True}
        return result
    if state["state_revision"] != expected_revision:
        raise IntegrityError(
            f"Stale state revision: expected {expected_revision}, current {state['state_revision']}"
        )
    safe_argv = _redact_argv(command["argv"])
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "validation": {
                "command_id": command_id,
                "argv": safe_argv,
                "cwd": command["cwd"],
                "timeout_seconds": command["timeout_seconds"],
                "max_output_bytes": command["max_output_bytes"],
                "environment_keys": sorted(allowed_environment(command["env_allow"])),
            },
        }
    cache_dir = root / ".dls" / "cache" / "validation" / change_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_path = cache_dir / f"{effective_operation_id}.log"
    try:
        execution = _run_bounded_command(
            command["argv"],
            cwd=cwd,
            environment=allowed_environment(command["env_allow"]),
            timeout_seconds=command["timeout_seconds"],
            max_output_bytes=command["max_output_bytes"],
        )
        output_text = redact_text(execution["output"].decode("utf-8", errors="replace"))
        atomic_write_text(output_path, output_text, backup=False)
        summary = (
            f"command={command_id}; exit={execution['exit_code']}; "
            f"timeout={execution['timed_out']}; output_bytes={execution['output_bytes']}; "
            f"output_overflow={execution['overflow']}\n{output_text}"
        )
        evidence = evidence_add(
            root,
            change_id=change_id,
            command_id=command_id,
            exit_code=execution["exit_code"],
            summary=summary,
            expected_revision=expected_revision,
            git_sha=git_head(root),
            artifacts=[],
            environment=f"cwd={command['cwd']}",
            duration_seconds=execution["duration_seconds"],
            operation_id=effective_operation_id,
            extra={
                "argv": safe_argv,
                "timed_out": execution["timed_out"],
                "output_bytes": execution["output_bytes"],
                "output_overflow": execution["overflow"],
            },
        )
        evidence["validation"] = {
            "timed_out": execution["timed_out"],
            "output_overflow": execution["overflow"],
            "redacted_log_path": str(output_path.relative_to(root)),
        }
        evidence["ok"] = (
            execution["exit_code"] == 0
            and not execution["timed_out"]
            and not execution["overflow"]
        )
        return evidence
    except Exception:
        output_path.unlink(missing_ok=True)
        raise


def _validate_scoped_confirmation(
    decision: str,
    object_digest: str,
    prompt: str | None,
    response: str | None,
) -> None:
    if not prompt or not response:
        raise IntegrityError("Codex-recorded approval requires the scoped prompt and user response")
    prompt_lower = prompt.lower()
    if decision.lower() not in prompt_lower or object_digest[:8] not in prompt_lower:
        raise IntegrityError("Approval prompt must name the decision and current short digest")
    if not AFFIRMATIVE_PATTERN.search(response):
        raise IntegrityError("User response is not an explicit scoped affirmation")


def _context_manifest_content_digest(manifest: dict[str, Any]) -> str:
    digest_basis = json.dumps(
        {
            "schema_version": manifest.get("schema_version"),
            "dls_version": manifest.get("dls_version"),
            "profile": manifest.get("profile"),
            "change_id": manifest.get("change_id"),
            "phase": manifest.get("phase"),
            "state_revision": manifest.get("state_revision"),
            "git_head": manifest.get("git_head"),
            "inputs": manifest.get("inputs"),
            "exclusions": manifest.get("exclusions"),
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    return sha256_bytes(digest_basis)


def _validate_review_report(
    report: dict[str, Any],
    change_id: str,
    pack: dict[str, Any],
) -> None:
    required = {
        "schema_version",
        "review_id",
        "change_id",
        "base_sha",
        "head_sha",
        "pack_digest",
        "definition_digest",
        "verdict",
        "lanes",
        "ticket_verdicts",
        "findings",
    }
    missing = sorted(required - report.keys())
    if missing:
        raise IntegrityError(f"Review report missing fields: {', '.join(missing)}")
    schema_version = report.get("schema_version")
    expected_schema = (
        REVIEW_IR_SCHEMA_VERSION
        if pack.get("schema_version") == REVIEW_PACK_SCHEMA_VERSION
        else 1
    )
    if schema_version != expected_schema or report["change_id"] != change_id:
        raise IntegrityError("Review report schema or change_id mismatch")
    if report["verdict"] not in REVIEW_VERDICTS:
        raise IntegrityError(f"Invalid review verdict: {report['verdict']}")
    lanes = report["lanes"]
    if not isinstance(lanes, dict) or not isinstance(lanes.get("semantic"), dict):
        raise IntegrityError("ReviewIR lanes must contain semantic provenance")
    semantic_lane = lanes["semantic"]
    required_semantic = {
        "status",
        "model",
        "reasoning_effort",
        "context_manifest_path",
        "context_manifest_digest",
        "independent_draft_path",
        "independent_draft_digest",
    }
    missing_semantic = sorted(required_semantic - semantic_lane.keys())
    if missing_semantic:
        raise IntegrityError(
            "ReviewIR semantic lane missing fields: " + ", ".join(missing_semantic)
        )
    if semantic_lane["status"] != "completed":
        raise IntegrityError("ReviewIR semantic lane must be completed")
    if semantic_lane["model"] != "gpt-5.6-sol":
        raise IntegrityError("ReviewIR semantic lane must use gpt-5.6-sol")
    if semantic_lane["reasoning_effort"] not in {"high", "xhigh"}:
        raise IntegrityError("ReviewIR semantic reasoning effort must be high or xhigh")
    for field in (
        "context_manifest_path",
        "context_manifest_digest",
        "independent_draft_path",
        "independent_draft_digest",
    ):
        if not isinstance(semantic_lane[field], str) or not semantic_lane[field]:
            raise IntegrityError(f"ReviewIR semantic lane missing {field}")
    if schema_version == REVIEW_IR_SCHEMA_VERSION:
        if report.get("review_mode") != pack["review_mode"]:
            raise IntegrityError("ReviewIR review_mode does not match ReviewPack")
        if report.get("comparison_base_sha") != pack["comparison_base_sha"]:
            raise IntegrityError("ReviewIR comparison base does not match ReviewPack")
        semantic_passes = semantic_lane.get("passes")
        if not isinstance(semantic_passes, list) or not semantic_passes:
            raise IntegrityError("ReviewIR v2 semantic lane requires passes")
        pass_kinds: list[str] = []
        for semantic_pass in semantic_passes:
            if not isinstance(semantic_pass, dict):
                raise IntegrityError("ReviewIR semantic pass must be an object")
            kind = semantic_pass.get("kind")
            if kind not in {"full", "targeted", "final-full"} or kind in pass_kinds:
                raise IntegrityError(f"Invalid or duplicate semantic pass: {kind!r}")
            if semantic_pass.get("status") != "completed":
                raise IntegrityError(f"Semantic pass is not completed: {kind}")
            for field in ("draft_path", "draft_digest"):
                if not isinstance(semantic_pass.get(field), str) or not semantic_pass[field]:
                    raise IntegrityError(f"Semantic pass {kind} missing {field}")
            pass_kinds.append(kind)
        if pack["review_mode"] == "full" and pass_kinds != ["full"]:
            raise IntegrityError("Full review requires exactly one full semantic pass")
        if pack["review_mode"] == "remediation":
            if not pass_kinds or pass_kinds[0] != "targeted":
                raise IntegrityError("Remediation review must start with targeted semantic pass")
            if report["verdict"] == "review-clear" and pass_kinds != [
                "targeted",
                "final-full",
            ]:
                raise IntegrityError(
                    "Remediation review-clear requires targeted then final-full semantic passes"
                )
            if report["verdict"] != "review-clear" and pass_kinds not in (
                ["targeted"],
                ["targeted", "final-full"],
            ):
                raise IntegrityError("Invalid remediation semantic pass sequence")
        specialists = lanes.get("specialists", [])
        if not isinstance(specialists, list):
            raise IntegrityError("ReviewIR specialist lanes must be an array")
        expected_lenses = [item["id"] for item in pack["risk_lenses"]]
        actual_lenses: list[str] = []
        for specialist in specialists:
            if not isinstance(specialist, dict):
                raise IntegrityError("Specialist lane must be an object")
            lens_id = specialist.get("lens_id")
            if (
                not isinstance(lens_id, str)
                or lens_id in actual_lenses
                or specialist.get("status") != "completed"
            ):
                raise IntegrityError(f"Invalid specialist lane: {lens_id!r}")
            for field in ("draft_path", "draft_digest"):
                if not isinstance(specialist.get(field), str) or not specialist[field]:
                    raise IntegrityError(f"Specialist lane {lens_id} missing {field}")
            actual_lenses.append(lens_id)
        if actual_lenses != expected_lenses:
            raise IntegrityError(
                f"ReviewIR specialist lanes mismatch: expected {expected_lenses}"
            )
    if not isinstance(report["findings"], list):
        raise IntegrityError("Review findings must be an array")
    seen: set[str] = set()
    for finding in report["findings"]:
        if not isinstance(finding, dict):
            raise IntegrityError("Each finding must be an object")
        finding_id = finding.get("id")
        if not isinstance(finding_id, str) or not finding_id or finding_id in seen:
            raise IntegrityError(f"Invalid or duplicate finding ID: {finding_id!r}")
        seen.add(finding_id)
        if finding.get("severity") not in FINDING_SEVERITIES:
            raise IntegrityError(f"Invalid severity for {finding_id}")
        if finding.get("kind") not in FINDING_KINDS:
            raise IntegrityError(f"Invalid kind for {finding_id}")
        for field in ("location", "issue", "impact", "required_fix", "base_sha", "head_sha"):
            if not isinstance(finding.get(field), str) or not finding[field].strip():
                raise IntegrityError(f"Finding {finding_id} missing {field}")
        for field in ("ticket_ids", "requirement_ids"):
            values = finding.get(field)
            if not isinstance(values, list) or not all(
                isinstance(item, str) and item for item in values
            ) or len(values) != len(set(values)):
                raise IntegrityError(f"Finding {finding_id} has invalid {field}")
        blocks = finding.get("blocks")
        if blocks is not None and (
            not isinstance(blocks, list)
            or not blocks
            or not all(item in REVIEW_BLOCK_STAGES for item in blocks)
            or len(blocks) != len(set(blocks))
        ):
            raise IntegrityError(f"Finding {finding_id} has invalid blocks")
        unknown_tickets = sorted(set(finding["ticket_ids"]) - set(pack["tickets"]))
        if unknown_tickets:
            raise IntegrityError(
                f"Finding {finding_id} references unknown tickets: "
                + ", ".join(unknown_tickets)
            )
        if finding["base_sha"] != report["base_sha"] or finding["head_sha"] != report["head_sha"]:
            raise IntegrityError(f"Finding {finding_id} base/head mismatch")
    ticket_verdicts = report["ticket_verdicts"]
    if not isinstance(ticket_verdicts, list):
        raise IntegrityError("ReviewIR ticket_verdicts must be an array")
    verdict_by_ticket: dict[str, dict[str, Any]] = {}
    for item in ticket_verdicts:
        if not isinstance(item, dict):
            raise IntegrityError("Each ticket verdict must be an object")
        ticket_id = item.get("ticket_id")
        if (
            not isinstance(ticket_id, str)
            or ticket_id not in pack["tickets"]
            or ticket_id in verdict_by_ticket
        ):
            raise IntegrityError(f"Invalid or duplicate ticket verdict: {ticket_id!r}")
        if item.get("verdict") not in REVIEW_TICKET_VERDICTS:
            raise IntegrityError(f"Invalid verdict for ticket {ticket_id}")
        finding_ids = item.get("finding_ids")
        if not isinstance(finding_ids, list) or not all(
            isinstance(finding_id, str) and finding_id in seen
            for finding_id in finding_ids
        ) or len(finding_ids) != len(set(finding_ids)):
            raise IntegrityError(f"Invalid finding_ids for ticket {ticket_id}")
        verdict_by_ticket[ticket_id] = item
    missing_tickets = sorted(set(pack["tickets"]) - set(verdict_by_ticket))
    if missing_tickets:
        raise IntegrityError(
            "ReviewIR is missing ticket verdicts: " + ", ".join(missing_tickets)
        )
    for ticket_id, item in verdict_by_ticket.items():
        expected_findings = {
            finding["id"]
            for finding in report["findings"]
            if ticket_id in finding["ticket_ids"]
        }
        if set(item["finding_ids"]) != expected_findings:
            raise IntegrityError(
                f"Ticket {ticket_id} finding_ids do not match finding ticket_ids"
            )
        has_review_blocker = any(
            finding["id"] in expected_findings
            and finding["severity"] in {"blocker", "should-fix"}
            and "review" in _finding_blocks(finding)
            for finding in report["findings"]
        )
        if item["verdict"] == "clear" and has_review_blocker:
            raise IntegrityError(
                f"Ticket {ticket_id} cannot be clear with review-blocking findings"
            )
        if item["verdict"] == "not-clear" and not has_review_blocker:
            raise IntegrityError(
                f"Ticket {ticket_id} not-clear has no review-blocking finding"
            )
    dispositions = report.get("dispositions", [])
    if not isinstance(dispositions, list):
        raise IntegrityError("Review dispositions must be an array")
    if dispositions:
        raise IntegrityError("ReviewIR cannot mutate finding dispositions; use dls finding set")
    if schema_version == REVIEW_IR_SCHEMA_VERSION:
        prior_verdicts = report.get("prior_finding_verdicts")
        if not isinstance(prior_verdicts, list):
            raise IntegrityError("ReviewIR v2 requires prior_finding_verdicts")
        required_prior = {
            item["finding_id"]: item
            for item in pack["required_prior_findings"]
        }
        actual_prior: dict[str, dict[str, Any]] = {}
        for item in prior_verdicts:
            if not isinstance(item, dict):
                raise IntegrityError("Prior finding verdict must be an object")
            finding_id = item.get("finding_id")
            if (
                not isinstance(finding_id, str)
                or finding_id not in required_prior
                or finding_id in actual_prior
            ):
                raise IntegrityError(
                    f"Invalid or duplicate prior finding verdict: {finding_id!r}"
                )
            verdict = item.get("verdict")
            if verdict not in PRIOR_FINDING_VERDICTS:
                raise IntegrityError(f"Invalid prior finding verdict for {finding_id}")
            evidence = item.get("evidence")
            if not isinstance(evidence, list) or not all(
                isinstance(value, str) and value for value in evidence
            ):
                raise IntegrityError(f"Prior finding verdict {finding_id} needs evidence")
            replacement = item.get("replacement_finding_id")
            if verdict in {"still-open", "regressed"}:
                if not isinstance(replacement, str) or replacement not in seen:
                    raise IntegrityError(
                        f"Prior finding {finding_id} requires a replacement finding"
                    )
            elif replacement is not None:
                raise IntegrityError(
                    f"Prior finding {finding_id} cannot declare a replacement"
                )
            if verdict == "waived":
                disposition = required_prior[finding_id].get("disposition")
                if not isinstance(disposition, dict) or disposition.get("status") != "waived":
                    raise IntegrityError(
                        f"Prior finding {finding_id} has no current human waiver"
                    )
            actual_prior[finding_id] = item
        missing_prior = sorted(set(required_prior) - set(actual_prior))
        if missing_prior:
            raise IntegrityError(
                "ReviewIR is missing prior finding verdicts: "
                + ", ".join(missing_prior)
            )
        if report["verdict"] == "review-clear" and any(
            item["verdict"] in {"still-open", "regressed"}
            for item in actual_prior.values()
        ):
            raise IntegrityError(
                "ReviewIR cannot be clear with open or regressed prior findings"
            )
    review_blocking = [
        finding
        for finding in report["findings"]
        if finding["severity"] in {"blocker", "should-fix"}
        and "review" in _finding_blocks(finding)
    ]
    ticket_verdict_values = {
        item["verdict"] for item in verdict_by_ticket.values()
    }
    expected_verdict = "review-clear"
    if "blocked" in ticket_verdict_values:
        expected_verdict = "blocked"
    elif review_blocking or "not-clear" in ticket_verdict_values:
        expected_verdict = "not-clear"
    if report["verdict"] != expected_verdict:
        raise IntegrityError(
            f"ReviewIR verdict is inconsistent: expected {expected_verdict}"
        )


def _latest_review_result(state: dict[str, Any]) -> dict[str, Any] | None:
    for entry in reversed(state["reviews"]):
        if entry.get("kind") == "result":
            return entry
    return None


def _open_finding_counts(root: Path, state: dict[str, Any]) -> dict[str, int]:
    findings = _all_review_findings(root, state)
    if not findings:
        return {"blocker": 0, "should-fix": 0, "note": 0}
    latest_dispositions = _latest_dispositions(state)
    closed = {
        finding_id
        for finding_id, disposition in latest_dispositions.items()
        if disposition["status"] in {"verified", "waived"}
    }
    closed.update(_superseded_prior_finding_ids(root, state))
    open_findings = [
        item
        for finding_id, item in findings.items()
        if finding_id not in closed and "acceptance" in _finding_blocks(item)
    ]
    return _finding_counts(open_findings)


def _successful_evidence_for_current_revision(
    root: Path,
    state: dict[str, Any],
    *,
    stage: str,
) -> tuple[bool, str]:
    ok, detail, _ = _required_evidence_status(root, state, stage=stage)
    return ok, detail


def _finding_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"blocker": 0, "should-fix": 0, "note": 0}
    for finding in findings:
        severity = finding.get("severity")
        if severity in counts:
            counts[severity] += 1
    return counts


def _finding_blocks(finding: dict[str, Any]) -> set[str]:
    blocks = finding.get("blocks")
    if blocks is None:
        return {"review", "acceptance"}
    if not isinstance(blocks, list):
        return set()
    return {item for item in blocks if item in REVIEW_BLOCK_STAGES}


def _global_conflict_inventory() -> dict[str, Any]:
    home = Path.home()
    config_path = home / ".codex" / "config.toml"
    agents_path = home / ".codex" / "agents"
    configured_plugin_keys: list[str] = []
    if config_path.is_file():
        try:
            config = __import__("tomllib").loads(config_path.read_text(encoding="utf-8"))
            plugins = config.get("plugins", {})
            if isinstance(plugins, dict):
                configured_plugin_keys = sorted(
                    key
                    for key, value in plugins.items()
                    if isinstance(value, dict)
                    and value.get("enabled") is True
                    and (
                        "superpowers" in key
                        or "ios-development-workflow" in key
                    )
                )
        except Exception:
            configured_plugin_keys = ["unreadable-global-config"]
    enabled_plugin_keys: list[str] = []
    plugin_inventory_status = "unavailable"
    try:
        result = subprocess.run(
            ["codex", "plugin", "list", "--json"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        if result.returncode == 0:
            payload = json.loads(result.stdout)
            installed = payload.get("installed", [])
            if isinstance(installed, list):
                enabled_plugin_keys = sorted(
                    item["pluginId"]
                    for item in installed
                    if isinstance(item, dict)
                    and item.get("enabled") is True
                    and isinstance(item.get("pluginId"), str)
                    and (
                        "superpowers" in item["pluginId"]
                        or "ios-development-workflow" in item["pluginId"]
                    )
                )
                plugin_inventory_status = "ok"
        else:
            plugin_inventory_status = "command-failed"
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        plugin_inventory_status = "unavailable"
    agents = sorted(path.stem for path in agents_path.glob("*.toml")) if agents_path.is_dir() else []
    return {
        "configured_legacy_process_plugins": configured_plugin_keys,
        "enabled_legacy_process_plugins": enabled_plugin_keys,
        "plugin_inventory_status": plugin_inventory_status,
        "custom_agent_count": len(agents),
        "custom_agents": agents,
    }


def _check(check_id: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"id": check_id, "ok": ok, "detail": detail}


def _requirement_id_pattern(state: dict[str, Any]) -> re.Pattern[str]:
    prefixes = state.get("requirement_prefixes", [])
    if not isinstance(prefixes, list) or not all(
        isinstance(prefix, str) and REQUIREMENT_PREFIX_PATTERN.fullmatch(prefix)
        for prefix in prefixes
    ):
        raise IntegrityError("state.requirement_prefixes must contain valid prefixes")
    alternatives = [r"REQ-[0-9]{3,}"]
    alternatives.extend(
        rf"{re.escape(prefix)}-[0-9]{{2,}}" for prefix in sorted(set(prefixes))
    )
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b")


def _traceability_requirement_ids(
    text: str,
    *,
    declared_ticket_ids: set[str],
    requirement_pattern: re.Pattern[str],
) -> set[str]:
    payload = json.loads(text)
    linked: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            producer = value.get("producerTicket")
            if isinstance(producer, str) and producer in declared_ticket_ids:
                linked.update(
                    key
                    for key in value
                    if isinstance(key, str) and requirement_pattern.fullmatch(key)
                )
            for key, child in value.items():
                if (
                    isinstance(key, str)
                    and requirement_pattern.fullmatch(key)
                    and isinstance(child, dict)
                    and child.get("producerTicket") in declared_ticket_ids
                ):
                    linked.add(key)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return linked


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise UsageError("Slug must contain at least one ASCII letter or digit")
    return slug[:64]


def _has_operation(state: dict[str, Any], operation_id: str) -> bool:
    return _operation(state, operation_id) is not None


def _operation(state: dict[str, Any], operation_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in state["operations"]
            if isinstance(item, dict) and item.get("id") == operation_id
        ),
        None,
    )


def _require_operation_kind(operation: dict[str, Any], expected_kind: str) -> None:
    if operation.get("kind") != expected_kind:
        raise IntegrityError(
            f"Operation ID already belongs to {operation.get('kind')}: {operation.get('id')}"
        )


def _require_revision(state: dict[str, Any], expected_revision: int) -> None:
    if state["state_revision"] != expected_revision:
        raise IntegrityError(
            f"Stale state revision: expected {expected_revision}, current {state['state_revision']}"
        )


def _redact_argv(argv: list[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    sensitive_flag = re.compile(r"(?i)^--?(api[_-]?key|token|password|secret|dsn)$")
    sensitive_assignment = re.compile(
        r"(?i)^(--?(?:api[_-]?key|token|password|secret|dsn))=(.*)$"
    )
    for argument in argv:
        if hide_next:
            redacted.append("[REDACTED]")
            hide_next = False
            continue
        if sensitive_flag.fullmatch(argument):
            redacted.append(argument)
            hide_next = True
            continue
        assignment = sensitive_assignment.fullmatch(argument)
        if assignment:
            redacted.append(f"{assignment.group(1)}=[REDACTED]")
            continue
        redacted.append(redact_text(argument))
    return redacted


def _run_bounded_command(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
    terminate_on_overflow: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=True,
            bufsize=0,
        )
    except OSError as exc:
        message = f"Unable to start command: {exc}".encode("utf-8", errors="replace")
        return {
            "exit_code": 127,
            "timed_out": False,
            "overflow": len(message) > max_output_bytes,
            "output": message[:max_output_bytes],
            "output_bytes": len(message),
            "duration_seconds": time.monotonic() - started,
        }
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    retained = bytearray()
    total = 0
    timed_out = False
    overflow = False

    def stop_process() -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            process.kill()

    try:
        while selector.get_map():
            elapsed = time.monotonic() - started
            if elapsed >= timeout_seconds and not timed_out:
                timed_out = True
                stop_process()
            wait_for = max(0.0, min(0.1, timeout_seconds - elapsed))
            events = selector.select(wait_for)
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
                remaining = max_output_bytes - len(retained)
                if remaining > 0:
                    retained.extend(chunk[:remaining])
                if total > max_output_bytes and not overflow:
                    overflow = True
                    if terminate_on_overflow:
                        stop_process()
        process.wait()
    finally:
        selector.close()
        process.stdout.close()
        stop_process()
    exit_code = process.returncode if process.returncode is not None else 1
    if timed_out:
        exit_code = 124
    elif overflow and terminate_on_overflow:
        exit_code = 125
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "overflow": overflow,
        "output": bytes(retained),
        "output_bytes": total,
        "duration_seconds": time.monotonic() - started,
    }
