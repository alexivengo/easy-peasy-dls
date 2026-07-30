"""DLS command operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION, VERSION
from .decisions import (
    ARCHITECTURE_DIGEST_CONTRACT,
    DESIGN_DIGEST_CONTRACT,
    architecture_digest,
    architecture_source,
    build_design_source,
    decision_projection,
    decision_readiness,
    design_digest,
    review_pack_decisions_current,
)
from .errors import ConfigError, IntegrityError, UsageError
from .economy import processed_tokens, review_budget, token_budget_failure
from .io import (
    atomic_write_json,
    atomic_write_text,
    canonical_file_digest,
    canonical_text,
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
    PROFILE_CONTRACT,
    PROFILES_ROOT,
    SCHEMAS_ROOT,
    TEMPLATES_ROOT,
    allowed_environment,
    artifact_paths_matching_revision,
    command_config,
    command_contract_digest,
    copy_asset,
    git_changed_files,
    git_head,
    git_merge_base,
    git_product_tree_digest,
    git_source_dirty_paths,
    git_source_snapshot_digest,
    is_git_repo,
    load_config,
    resolve_profile,
    render_template,
    run_git,
)
from .state import (
    CONTROL_LEVELS,
    DEFINITION_DIGEST_CONTRACT,
    DEFINITION_DECISIONS_CONTRACT,
    IMPACT_TAGS,
    WORK_KINDS,
    StateStore,
    artifact_role,
    current_definition_digest,
    definition_artifacts,
    derived_approval_statuses,
    initial_state,
    validate_change_id,
)
from .worktrees import (
    owner_preparation_required,
    resolve_change_root,
)

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
NATIVE_REVIEW_TRANSCRIPT_MAX_BYTES = 1048576
LEGACY_REVIEW_RUNNER_CONTRACT = "dls-review-runner/v1"
REVIEW_RUNNER_CONTRACT = "dls-review-runner/v2"
REVIEW_CONTEXT_CONTRACT = "dls-review-context/v2"
REVIEW_ECONOMY_CONTRACT = "dls-review-economy/v1"
REVIEW_BUDGET_CONTRACT = "dls-review-budget/v2"
COMMAND_EVENT_CONTRACT = "logical-invocations/v1"
NATIVE_OUTPUT_CONTRACT = "dls-native-review/v2"
NATIVE_WORKSPACE_CONTRACT = "dls-native-workspace/v1"
NATIVE_WORKSPACE_ISOLATION = "standalone-clone"
NATIVE_INDETERMINATE_PROJECTION_CONTRACT = (
    "dls-native-plaintext-indeterminate/v1"
)
NATIVE_TRANSCRIPT_VALIDATION_CONTRACT = "dls-native-transcript-final-message/v1"
REVIEW_IDENTIFIER_CONTRACT = "canonical-ticket-ids/v1"
REVIEW_DECISION_REPAIR_CONTRACT = "dls-decision-repair/v1"
REVIEW_LANE_MAX_ATTEMPTS = 2
RETRYABLE_REVIEW_LANE_STATUSES = {
    "abandoned",
    "api-failure",
    "incompatible-workspace",
    "timeout",
    "output-cap",
    "missing-output",
}

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
    root = resolve_change_root(root, change_id)
    state = StateStore(root).load(change_id)
    definition_digest = current_definition_digest(root, state)
    approvals = derived_approval_statuses(root, state)
    decisions = decision_readiness(
        root,
        state,
        approvals,
        require_definition=state["control_level"] in {"standard", "critical"},
    )
    head = git_head(root)
    dirty = git_source_dirty_paths(root) if is_git_repo(root) else []
    latest_review = _latest_review_result(state)
    review_stale = bool(latest_review and latest_review.get("head_sha") != head)
    current_acceptance = next(
        (
            item
            for item in reversed(approvals)
            if item.get("decision") == "accept" and item.get("status") == "current"
        ),
        None,
    )
    if (
        review_stale
        and current_acceptance is not None
        and latest_review is not None
        and latest_review.get("head_sha") == current_acceptance.get("git_sha")
    ):
        # Acceptance proves the reviewed product tree.  A later DLS-only commit
        # does not make that terminal review stale.
        review_stale = False
    from .parallel_delivery import change_readiness

    stage = (
        "definition"
        if state.get("phase") == "definition"
        else "acceptance"
        if state.get("phase") == "accepted"
        else "review"
        if state.get("phase") == "review"
        else "implementation"
    )
    readiness = change_readiness(
        root,
        change_id=change_id,
        stage=stage,
        include_overlap=stage in {"review", "acceptance"},
    )
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
        "decisions": decisions["decisions"],
        "decision_next_action": decisions["next_action"],
        "next_action": decisions["next_action"] or readiness["next_action"],
        "tickets": state["tickets"],
        "evidence_count": len(state["evidence"]),
        "latest_review": latest_review,
        "review_stale": review_stale,
        "git_head": head,
        "source_dirty_paths": dirty,
        "dependencies": readiness["dependencies"],
        "parallelism": {
            "ready": readiness["ready"],
            "overlap": readiness["overlap"],
            "next_action": readiness["next_action"],
        },
    }


def check(root: Path, *, change_id: str, gate: str) -> dict[str, Any]:
    if gate not in {"definition", "review", "accept", "all"}:
        raise UsageError(f"Unknown gate: {gate}")
    root = resolve_change_root(root, change_id)
    state = StateStore(root).load(change_id)
    checks: list[dict[str, Any]] = []
    from .parallel_delivery import change_readiness

    dependency_stage = (
        "acceptance" if gate in {"accept", "all"} else "review" if gate == "review" else "definition"
    )
    dependency_readiness = change_readiness(
        root,
        change_id=change_id,
        stage=dependency_stage,
        include_overlap=dependency_stage in {"review", "acceptance"},
    )
    checks.append(
        _check(
            f"dependencies:{dependency_stage}",
            dependency_readiness["ready"],
            (
                dependency_readiness["next_action"]["detail"]
                if dependency_readiness["next_action"]
                else "ready"
            ),
        )
    )
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
        if artifact_role(key, state["artifacts"][key]) == "execution":
            continue
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
    decisions = decision_projection(root, state, approvals)
    decision_gate = decision_readiness(
        root,
        state,
        approvals,
        require_definition=state["control_level"] in {"standard", "critical"},
    )
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
    current_acceptance = next(
        (
            item
            for item in reversed(approvals)
            if item.get("decision") == "accept" and item.get("status") == "current"
        ),
        None,
    )
    accepted_head = (
        current_acceptance.get("git_sha")
        if current_acceptance is not None
        else None
    )
    latest_review = _latest_review_result(state)
    review_clear = bool(
        latest_review
        and latest_review.get("verdict") == "review-clear"
        and latest_review.get("head_sha") in {git_head(root), accepted_head}
    )
    acceptance_grade_review = bool(
        review_clear and latest_review and latest_review.get("mode") == "acceptance-grade"
    )
    source_dirty = git_source_dirty_paths(root) if is_git_repo(root) else ["not-a-git-repository"]
    strict_path = state["control_level"] in {"standard", "critical"}
    if gate == "definition":
        if decisions["design"]["required"]:
            checks.append(
                _check(
                    "ui:design-source",
                    decisions["design"]["contract"] is not None,
                    "typed source or explicit bypass required",
                )
            )
        if decisions["architecture"]["required"]:
            checks.append(
                _check(
                    "architecture:source",
                    decisions["architecture"]["digest"] is not None,
                    "bounded ADR or SPEC decision required",
                )
            )
    if gate in {"review", "accept", "all"}:
        if strict_path:
            checks.append(
                _check("definition:approved", definition_approved, "current approval required")
            )
        if decisions["design"]["required"]:
            checks.append(
                _check(
                    "ui:design-source",
                    decisions["design"]["contract"] is not None,
                    "typed source or explicit bypass required",
                )
            )
            checks.append(
                _check(
                    "ui:design-decision",
                    design_approved,
                    "current scoped design approval required",
                )
            )
        if decisions["architecture"]["required"]:
            checks.append(
                _check(
                    "architecture:source",
                    decisions["architecture"]["digest"] is not None,
                    "bounded ADR or SPEC decision required",
                )
            )
            checks.append(
                _check(
                    "architecture:decision",
                    architecture_approved,
                    "current scoped architecture approval required",
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
                proof_head_sha=accepted_head,
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
                proof_head_sha=accepted_head,
            )
            checks.append(_check("validation:passing-evidence", evidence_ok, evidence_detail))
        if strict_path:
            evidence_ok, evidence_detail = _successful_evidence_for_current_revision(
                root,
                state,
                stage="acceptance",
                proof_head_sha=accepted_head,
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
        "next_action": decision_gate["next_action"],
    }


def design_set(
    root: Path,
    *,
    change_id: str,
    tier: int,
    surfaces: list[str],
    source_kind: str | None,
    source_ref: str | None,
    source_version: str | None,
    bypass: bool,
    rationale: str | None,
    risk: str | None,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    root = resolve_change_root(root, change_id)
    store = StateStore(root)
    state = store.load(change_id)
    if "user-interface" not in state.get("impact_tags", []):
        raise UsageError("Design source is only available for user-interface changes")
    value = build_design_source(
        root,
        tier=tier,
        surfaces=surfaces,
        source_kind=source_kind,
        source_ref=source_ref,
        source_version=source_version,
        bypass=bypass,
        rationale=rationale,
        risk=risk,
    )
    candidate_state = {**state, "design_source": value}
    digest = design_digest(root, candidate_state)
    assert digest is not None
    effective_operation_id = operation_id or str(uuid.uuid4())
    operation_kind = "design-source-set"
    existing_operation = _operation(state, effective_operation_id)
    if existing_operation:
        _require_operation_kind(existing_operation, operation_kind)
        current_digest = design_digest(root, state)
        if current_digest != digest:
            raise IntegrityError("Design source operation ID belongs to another contract")
        return design_status(root, change_id=change_id) | {
            "dry_run": False,
            "changed": False,
            "operation_id": effective_operation_id,
        }
    if dry_run:
        approvals = derived_approval_statuses(root, candidate_state)
        readiness = decision_readiness(
            root,
            candidate_state,
            approvals,
            require_definition=candidate_state["control_level"]
            in {"standard", "critical"},
        )
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "design": readiness["decisions"]["design"],
            "next_action": readiness["next_action"],
        }

    def mutate(updated: dict[str, Any]) -> None:
        updated["design_source"] = value

    updated, changed = store.mutate(
        change_id,
        expected_revision=state["state_revision"],
        operation_id=effective_operation_id,
        operation_kind=operation_kind,
        mutator=mutate,
    )
    result = design_status(root, change_id=change_id)
    result.update(
        {
            "dry_run": False,
            "changed": changed,
            "state_revision": updated["state_revision"],
            "operation_id": effective_operation_id,
        }
    )
    return result


def design_status(root: Path, *, change_id: str) -> dict[str, Any]:
    root = resolve_change_root(root, change_id)
    state = StateStore(root).load(change_id)
    approvals = derived_approval_statuses(root, state)
    readiness = decision_readiness(
        root,
        state,
        approvals,
        require_definition=state["control_level"] in {"standard", "critical"},
    )
    return {
        "ok": True,
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "design": readiness["decisions"]["design"],
        "next_action": readiness["next_action"],
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
    include_design: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    if decision not in {"definition", "accept", "exception", "design", "architecture"}:
        raise UsageError(f"Invalid approval decision: {decision}")
    if actor not in {"codex", "user"}:
        raise UsageError("actor must be codex or user")
    if include_design and decision != "definition":
        raise UsageError("--include-design is available only with --decision definition")
    root = resolve_change_root(root, change_id)
    state_store = StateStore(root)
    state = state_store.load(change_id)
    effective_operation_id = operation_id or str(uuid.uuid4())
    decisions = [decision, "design"] if include_design else [decision]
    operation_kind = "approve:" + "+".join(decisions)
    approval_ids = {
        item: str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"dls:{change_id}:approval:{item}:{effective_operation_id}",
            )
        )
        for item in decisions
    }
    existing_operation = _operation(state, effective_operation_id)
    if existing_operation:
        _require_operation_kind(existing_operation, operation_kind)
        recorded = [
            item for item in state["approvals"] if item.get("id") in approval_ids.values()
        ]
        if len(recorded) != len(decisions):
            raise IntegrityError(f"Approval operation has no matching record: {effective_operation_id}")
        result = {
            "ok": True,
            "dry_run": False,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "approval": next(item for item in recorded if item["decision"] == decision),
            "approvals": recorded,
        }
        if decision == "accept":
            from .delivery_receipt import delivery_receipt
            result["delivery_receipt"] = delivery_receipt(root, change_id=change_id)
        return result
    _require_revision(state, expected_revision)
    if state["lifecycle"] == "accepted" and decision != "exception":
        raise IntegrityError("Accepted work cannot receive another decision without a new change")
    definition_digest = current_definition_digest(root, state)
    design_decision_digest = design_digest(root, state)
    architecture_decision_digest = architecture_digest(root, state)
    if decision == "definition" and "user-interface" in state.get("impact_tags", []):
        if design_decision_digest is None:
            raise IntegrityError("Definition approval requires a typed design source or bypass")
    if decision == "definition":
        current_approvals = derived_approval_statuses(root, state)
        architecture_gate = decision_projection(root, state, current_approvals)[
            "architecture"
        ]
        if architecture_gate["required"] and architecture_decision_digest is None:
            raise IntegrityError("Definition approval requires a bounded architecture decision")
        if architecture_gate["required"] and architecture_gate["approval"] != "current":
            raise IntegrityError("Definition approval requires current architecture approval")
    if "design" in decisions and design_decision_digest is None:
        raise IntegrityError("Design approval requires a current typed design source")
    if decision == "architecture" and architecture_decision_digest is None:
        raise IntegrityError("Architecture approval requires a bounded ADR or SPEC decision")
    object_digests = {
        "definition": definition_digest,
        "accept": definition_digest,
        "exception": definition_digest,
        "design": design_decision_digest,
        "architecture": architecture_decision_digest,
    }
    current_head = git_head(root)
    acceptance_source_digest: str | None = None
    if (
        decision == "definition"
        and state["control_level"] in {"standard", "critical"}
    ):
        if not current_head:
            raise IntegrityError(
                "Standard and critical definition approval requires Git"
            )
        if git_sha and git_sha != current_head:
            raise IntegrityError(
                f"Definition approval SHA is not current HEAD: {git_sha} != {current_head}"
            )
        reproducible, dirty_artifacts = artifact_paths_matching_revision(
            root,
            definition_artifacts(state),
            current_head,
        )
        if not reproducible:
            raise IntegrityError(
                "Definition approval requires committed authored artifacts: "
                + ", ".join(dirty_artifacts)
            )
        git_sha = current_head
    if decision in {"design", "architecture"}:
        if state["control_level"] in {"standard", "critical"} and not current_head:
            raise IntegrityError("Scoped decision approval requires Git")
        if git_sha and git_sha != current_head:
            raise IntegrityError(
                f"Scoped decision approval SHA is not current HEAD: {git_sha} != {current_head}"
            )
        if decision == "architecture" and current_head:
            source = architecture_source(root, state)
            if source is None:
                raise IntegrityError("Architecture decision source is missing")
            source_artifact = {
                "architecture": {"path": source["path"], "role": "definition"}
            }
            reproducible, dirty_artifacts = artifact_paths_matching_revision(
                root, source_artifact, current_head
            )
            if not reproducible:
                raise IntegrityError(
                    "Architecture approval requires a committed decision source: "
                    + ", ".join(dirty_artifacts)
                )
        git_sha = current_head
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
        acceptance_source_digest = git_product_tree_digest(root)
        if strict_path and not acceptance_source_digest:
            raise IntegrityError("Acceptance requires a readable product Git tree")
        gate = check(root, change_id=change_id, gate="accept")
        if not gate["ok"] and decision != "exception":
            failed = [item["id"] for item in gate["checks"] if not item["ok"]]
            raise IntegrityError(f"Acceptance gate failed: {', '.join(failed)}")
    if actor == "codex":
        for item in decisions:
            digest = object_digests[item]
            assert isinstance(digest, str)
            _validate_scoped_confirmation(item, digest, prompt, response)
    recorded_at = utc_now()
    approvals_to_record: list[dict[str, Any]] = []
    for item in decisions:
        digest = object_digests[item]
        assert isinstance(digest, str)
        approval: dict[str, Any] = {
            "id": approval_ids[item],
            "decision": item,
            "object_digest": digest,
            "git_sha": git_sha,
            "actor": actor,
            "authority": "user",
            "recorded_at": recorded_at,
            "status": "current",
            "conditions": conditions,
            "prompt": prompt,
            "response": response,
        }
        if item in {"definition", "accept"}:
            approval["definition_digest_contract"] = DEFINITION_DIGEST_CONTRACT
            approval["decision_snapshots_contract"] = DEFINITION_DECISIONS_CONTRACT
            approval["design_decision_digest"] = design_decision_digest
            approval["architecture_decision_digest"] = architecture_decision_digest
        elif item == "design":
            approval["decision_contract"] = DESIGN_DIGEST_CONTRACT
            approval["decision_digest"] = digest
        elif item == "architecture":
            approval["decision_contract"] = ARCHITECTURE_DIGEST_CONTRACT
            approval["decision_digest"] = digest
        if item == "accept" and acceptance_source_digest is not None:
            approval["source_digest"] = acceptance_source_digest
        approvals_to_record.append(approval)
    primary_approval = next(item for item in approvals_to_record if item["decision"] == decision)
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "approval": primary_approval,
            "approvals": approvals_to_record,
        }

    def mutate(value: dict[str, Any]) -> None:
        for approval in approvals_to_record:
            item_decision = approval["decision"]
            for existing in value["approvals"]:
                if existing.get("decision") == item_decision and existing.get("status") == "current":
                    existing["status"] = "superseded"
                    existing["superseded_by"] = approval["id"]
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
    recorded_approvals = [
        item for item in updated["approvals"] if item.get("id") in approval_ids.values()
    ]
    recorded_approval = next(
        item for item in recorded_approvals if item.get("decision") == decision
    )
    result = {
        "ok": True,
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "operation_id": effective_operation_id,
        "approval": recorded_approval,
        "approvals": recorded_approvals,
    }
    if decision == "accept":
        from .delivery_receipt import delivery_receipt

        result["delivery_receipt"] = delivery_receipt(root, change_id=change_id)
    return result


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
    *,
    proof_head_sha: str | None = None,
) -> dict[str, tuple[str, dict[str, Any]]]:
    current_head = git_head(root)
    evidence_head = proof_head_sha or current_head
    current_source_digest = git_source_snapshot_digest(root)
    latest: dict[str, tuple[str, dict[str, Any]]] = {}
    for relative, record in _evidence_records(root, state):
        command_id = record.get("command_id")
        if (
            not isinstance(command_id, str)
            or record.get("git_sha") != evidence_head
            or (
                evidence_head == current_head
                and record.get("source_digest") != current_source_digest
            )
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
    proof_head_sha: str | None = None,
) -> tuple[bool, str, list[str]]:
    config = load_config(root)
    policy = config.get("policy", {})
    key = (
        "review_required_commands"
        if stage == "review"
        else "acceptance_required_commands"
    )
    required = list(policy.get(key, []))
    latest = _current_evidence_by_command(
        root,
        state,
        proof_head_sha=proof_head_sha,
    )
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


def _canonical_review_findings(
    root: Path,
    state: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """Return the latest imported ReviewIR finding snapshot.

    ReviewIR v2 carries explicit prior-finding verdicts and replacement links.
    Historical v1 reports were whole-change reviews, so their newest imported
    result is also the only safe compatibility snapshot for current gates.
    """
    entry = _latest_review_result(state)
    if entry is None:
        return {}
    _, report = _read_review_result(root, entry)
    findings: dict[str, dict[str, Any]] = {}
    for finding in report.get("findings", []):
        finding_id = finding.get("id")
        if isinstance(finding_id, str):
            findings[finding_id] = finding
    return findings


def _active_prior_findings(
    root: Path,
    state: dict[str, Any],
    *,
    include_waived: bool = False,
) -> list[dict[str, Any]]:
    dispositions = _latest_dispositions(state)
    output: list[dict[str, Any]] = []
    for finding_id, finding in sorted(_canonical_review_findings(root, state).items()):
        if (
            finding.get("severity") not in {"blocker", "should-fix"}
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
    root = resolve_change_root(root, change_id)
    state = StateStore(root).load(change_id)
    approvals = derived_approval_statuses(root, state)
    decision_gate = decision_readiness(
        root,
        state,
        approvals,
        require_definition=state["control_level"] in {"standard", "critical"},
    )
    if not decision_gate["ready"]:
        return {
            "ok": True,
            "dry_run": dry_run,
            "changed": False,
            "change_id": change_id,
            "phase": phase,
            "status": "blocked",
            "manifest": None,
            "manifest_path": None,
            "decisions": decision_gate["decisions"],
            "next_action": decision_gate["next_action"],
        }
    if phase == "implementation" and state["control_level"] in {
        "standard",
        "critical",
    }:
        definition_approved = any(
            item.get("decision") == "definition" and item.get("status") == "current"
            for item in approvals
        )
        if not definition_approved:
            return {
                "ok": True,
                "dry_run": dry_run,
                "changed": False,
                "change_id": change_id,
                "phase": phase,
                "status": "blocked",
                "manifest": None,
                "manifest_path": None,
                "next_action": {
                    "id": "approve-definition",
                    "detail": current_definition_digest(root, state)[:12],
                },
            }
        if owner_preparation_required(root, change_id=change_id, state=state):
            return {
                "ok": True,
                "dry_run": dry_run,
                "changed": False,
                "change_id": change_id,
                "phase": phase,
                "status": "blocked",
                "manifest": None,
                "manifest_path": None,
                "next_action": {
                    "id": "prepare-owner-worktree",
                    "detail": "parallel standard/critical change requires an atomic owner handoff",
                },
            }
    from .parallel_delivery import change_readiness

    readiness_stage = "review" if phase == "review" else "implementation"
    readiness = change_readiness(
        root,
        change_id=change_id,
        stage=readiness_stage,
        include_overlap=phase == "review",
    )
    if not readiness["ready"]:
        return {
            "ok": True,
            "dry_run": dry_run,
            "changed": False,
            "change_id": change_id,
            "phase": phase,
            "status": "blocked",
            "manifest": None,
            "manifest_path": None,
            "dependencies": readiness["dependencies"],
            "parallelism": readiness["overlap"],
            "next_action": readiness["next_action"],
        }
    current_head = git_head(root)
    config = load_config(root)
    platform_profile = resolve_profile(root, config=config)
    profile = platform_profile["name"]
    selected: dict[str, str] = {}
    required_paths: set[str] = set()
    for key, metadata in state["artifacts"].items():
        if artifact_role(key, metadata) == "execution":
            continue
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
                and entry.get("head_sha") == current_head
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
    head = current_head
    digest_basis = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "dls_version": VERSION,
            "profile": profile,
            "platform_profile": platform_profile,
            "decisions": decision_gate["decisions"],
            "change_id": change_id,
            "phase": phase,
            "state_revision": state["state_revision"],
            "git_head": head,
            "inputs": inputs,
            "exclusions": sorted(excluded),
            "dependency_digest": readiness["dependencies"]["digest"],
            "overlap_digest": readiness["overlap"]["digest"],
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    manifest_digest = sha256_bytes(digest_basis)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "dls_version": VERSION,
        "profile": profile,
        "platform_profile": platform_profile,
        "decisions": decision_gate["decisions"],
        "change_id": change_id,
        "phase": phase,
        "generated_at": utc_now(),
        "manifest_digest": manifest_digest,
        "state_revision": state["state_revision"],
        "git_head": head,
        "inputs": inputs,
        "exclusions": sorted(excluded),
        "dependencies": {
            "contract": readiness["dependencies"]["contract"],
            "digest": readiness["dependencies"]["digest"],
            "satisfied": readiness["dependencies"]["satisfied"],
            "items": readiness["dependencies"]["items"],
        },
        "parallelism": {
            "contract": readiness["overlap"]["contract"],
            "digest": readiness["overlap"]["digest"],
            "exact_overlap_count": readiness["overlap"]["exact_overlap_count"],
            "proximity_count": readiness["overlap"]["proximity_count"],
        },
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


def _review_context_v2(
    root: Path,
    *,
    change_id: str,
    pack: dict[str, Any],
) -> dict[str, Any]:
    """Build a compact digest-bound review projection without full pack replay."""
    legacy = build_context(
        root,
        change_id=change_id,
        phase="review",
        include=[],
        exclude=[],
        dry_run=True,
    )["manifest"]
    context_root = root / ".dls" / "cache" / "context" / change_id
    context_root.mkdir(parents=True, exist_ok=True)
    projection_relative = (
        f".dls/cache/context/{change_id}/"
        f"review-pack-{pack['review_id']}-v2.json"
    )
    evidence: list[dict[str, Any]] = []
    for relative in pack.get("evidence", []):
        record = read_json(safe_resolve(root, relative, must_exist=True))
        evidence.append(
            {
                "path": relative,
                "command_id": record.get("command_id"),
                "git_sha": record.get("git_sha"),
                "source_digest": record.get("source_digest"),
                "exit_code": record.get("exit_code"),
                "record_digest": sha256_file(safe_resolve(root, relative, must_exist=True)),
            }
        )
    projection = {
        "contract": REVIEW_CONTEXT_CONTRACT,
        "review_id": pack["review_id"],
        "change_id": pack["change_id"],
        "review_mode": pack["review_mode"],
        "control_level": pack["control_level"],
        "epic_base_sha": pack["epic_base_sha"],
        "comparison_base_sha": pack["comparison_base_sha"],
        "head_sha": pack["head_sha"],
        "pack_digest": pack["pack_digest"],
        "definition_digest": pack["definition_digest"],
        "platform_profile": legacy.get("platform_profile"),
        "decisions": pack.get("decisions"),
        "tickets": pack["tickets"],
        "required_prior_findings": pack.get("required_prior_findings", []),
        "finding_dispositions": pack.get("finding_dispositions", []),
        "changed_files": pack.get("changed_files", []),
        "full_changed_files": pack.get("full_changed_files", []),
        "risk_lenses": pack.get("risk_lenses", []),
        "evidence": evidence,
    }
    atomic_write_json(safe_resolve(root, projection_relative), projection, backup=False)
    prior_requirement_ids = {
        requirement_id
        for finding in pack.get("required_prior_findings", [])
        for requirement_id in finding.get("requirement_ids", [])
        if isinstance(requirement_id, str)
    }
    ticket_ids = set(pack.get("tickets", {}))
    inputs: list[dict[str, Any]] = []
    for item in legacy["inputs"]:
        if item.get("reason") == "active-review-pack":
            continue
        relative = item["path"]
        if "requirement" in relative.lower():
            source = safe_resolve(root, relative, must_exist=True)
            text = source.read_text(encoding="utf-8", errors="replace")
            matched = [
                line
                for line in text.splitlines()
                if any(identifier in line for identifier in ticket_ids | prior_requirement_ids)
            ]
            snippet_relative = (
                f".dls/cache/context/{change_id}/"
                f"requirements-{pack['review_id']}-v2.json"
            )
            atomic_write_json(
                safe_resolve(root, snippet_relative),
                {
                    "contract": REVIEW_CONTEXT_CONTRACT,
                    "source_path": relative,
                    "source_digest": sha256_file(source),
                    "ticket_ids": sorted(ticket_ids),
                    "requirement_ids": sorted(prior_requirement_ids),
                    "matched_lines": matched,
                },
                backup=False,
            )
            snippet = safe_resolve(root, snippet_relative, must_exist=True)
            inputs.append(
                {
                    "path": snippet_relative,
                    "reason": "filtered-requirements-projection",
                    "sha256": sha256_file(snippet),
                    "bytes": snippet.stat().st_size,
                    "words": len(snippet.read_text(encoding="utf-8").split()),
                    "estimated_tokens": {"low": 0, "high": 0},
                }
            )
            continue
        inputs.append(item)
    projection_path = safe_resolve(root, projection_relative, must_exist=True)
    inputs.append(
        {
            "path": projection_relative,
            "reason": "active-review-pack",
            "projection_contract": REVIEW_CONTEXT_CONTRACT,
            "sha256": sha256_file(projection_path),
            "bytes": projection_path.stat().st_size,
            "words": len(projection_path.read_text(encoding="utf-8").split()),
            "estimated_tokens": {"low": 0, "high": 0},
        }
    )
    total_bytes = sum(int(item.get("bytes", 0)) for item in inputs)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "contract": REVIEW_CONTEXT_CONTRACT,
        "dls_version": VERSION,
        "profile": legacy["profile"],
        "platform_profile": legacy.get("platform_profile"),
        "decisions": pack.get("decisions"),
        "change_id": change_id,
        "phase": "review",
        "generated_at": utc_now(),
        "manifest_digest": "",
        "state_revision": legacy["state_revision"],
        "git_head": pack["head_sha"],
        "inputs": inputs,
        "exclusions": [],
        "context_mode": "large-context" if total_bytes > 512 * 1024 else "compact",
        "totals": {
            "bytes": total_bytes,
            "words": sum(int(item.get("words", 0)) for item in inputs),
            "estimated_tokens_low": sum(
                int(item.get("estimated_tokens", {}).get("low", 0)) for item in inputs
            ),
            "estimated_tokens_high": sum(
                int(item.get("estimated_tokens", {}).get("high", 0)) for item in inputs
            ),
        },
    }
    digest = _context_manifest_content_digest(manifest)
    manifest["manifest_digest"] = digest
    output = context_root / f"review-{digest[:12]}.json"
    atomic_write_json(output, manifest, backup=False)
    return {
        "manifest_path": str(output.relative_to(root)),
        "manifest": manifest,
    }


def _change_owner_root(root: Path, change_id: str) -> tuple[Path, str]:
    candidate = root.resolve()
    owner = resolve_change_root(candidate, change_id)
    return owner, "current-checkout" if owner == candidate else "registered-worktree"


def _canonical_remediation_manifest_path(change_id: str, review_id: str) -> str:
    return f".dls/reviews/{change_id}/remediations/{review_id}.json"


def _legacy_remediation_manifest_path(change_id: str, review_id: str) -> str:
    return f".dls/cache/context/{change_id}/remediation-{review_id}.json"


def _existing_remediation_manifest_path(
    root: Path,
    *,
    change_id: str,
    review_entry: dict[str, Any],
    review_id: str,
) -> str | None:
    candidates: list[str] = []
    linked = review_entry.get("remediation_manifest_path")
    if isinstance(linked, str) and linked:
        candidates.append(linked)
    candidates.extend(
        [
            _canonical_remediation_manifest_path(change_id, review_id),
            _legacy_remediation_manifest_path(change_id, review_id),
        ]
    )
    for relative in dict.fromkeys(candidates):
        if safe_resolve(root, relative).is_file():
            return relative
    return None


def _review_result_digest(report: dict[str, Any]) -> str:
    return sha256_bytes(
        json.dumps(
            report,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _review_actionable_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for finding in report.get("findings", []):
        if (
            not isinstance(finding, dict)
            or finding.get("severity") not in {"blocker", "should-fix"}
            or not ({"review", "acceptance"} & _finding_blocks(finding))
        ):
            continue
        finding_id = finding.get("id")
        if not isinstance(finding_id, str) or not finding_id:
            raise IntegrityError("Actionable review finding has no ID")
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
                "disposition": None,
            }
        )
    return sorted(output, key=lambda item: item["finding_id"])


def _json_file_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _input_record(
    *,
    relative: str,
    payload: bytes,
    reason: str,
    git_sha: str | None = None,
) -> dict[str, Any]:
    try:
        canonical_digest = sha256_bytes(
            canonical_text(payload.decode("utf-8")).encode("utf-8")
        )
    except UnicodeDecodeError:
        canonical_digest = sha256_bytes(payload)
    record: dict[str, Any] = {
        "path": relative,
        "reason": reason,
        "sha256": sha256_bytes(payload),
        "canonical_sha256": canonical_digest,
        "bytes": len(payload),
    }
    if git_sha is not None:
        record["git_sha"] = git_sha
    return record


def _git_blob_bytes(root: Path, git_sha: str, relative: str) -> bytes:
    safe_resolve(root, relative)
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{git_sha}:{relative}"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise IntegrityError(
            f"Reviewed Git tree is missing remediation input {relative}: {detail}"
        )
    return result.stdout


def _build_remediation_manifest(
    root: Path,
    *,
    change_id: str,
    review_entry: dict[str, Any],
    report: dict[str, Any],
    pack_entry: dict[str, Any],
    pack: dict[str, Any],
    result_path: str,
    pack_path: str,
    origin: str,
) -> dict[str, Any] | None:
    open_findings = _review_actionable_findings(report)
    if not open_findings:
        return None
    reviewed_head = report["head_sha"]
    inputs: list[dict[str, Any]] = [
        _input_record(
            relative=result_path,
            payload=_json_file_bytes(report),
            reason="canonical-review-result",
        ),
        _input_record(
            relative=pack_path,
            payload=safe_resolve(root, pack_path, must_exist=True).read_bytes(),
            reason="exact-revision-review-pack",
        ),
    ]
    for _, metadata in sorted(pack.get("artifacts", {}).items()):
        relative = metadata.get("path") if isinstance(metadata, dict) else None
        if not isinstance(relative, str) or not relative:
            raise IntegrityError("ReviewPack has invalid authored artifact metadata")
        inputs.append(
            _input_record(
                relative=relative,
                payload=_git_blob_bytes(root, reviewed_head, relative),
                reason="reviewed-authored-artifact",
                git_sha=reviewed_head,
            )
        )
    for relative in pack.get("evidence", []):
        if not isinstance(relative, str) or not relative:
            raise IntegrityError("ReviewPack has an invalid evidence path")
        inputs.append(
            _input_record(
                relative=relative,
                payload=safe_resolve(root, relative, must_exist=True).read_bytes(),
                reason="reviewed-validation-evidence",
            )
        )
    affected_paths: set[str] = set()
    for finding in open_findings:
        location = finding.get("location")
        if not isinstance(location, str) or not location:
            continue
        candidate = location.split(":", 1)[0]
        try:
            safe_resolve(root, candidate)
        except IntegrityError:
            continue
        affected_paths.add(candidate)
    manifest = {
        "schema_version": 2,
        "dls_version": VERSION,
        "origin": origin,
        "change_id": change_id,
        "review_id": report["review_id"],
        "review_result_path": result_path,
        "review_result_digest": review_entry.get("result_digest")
        or _review_result_digest(report),
        "review_pack_path": pack_path,
        "review_pack_digest": pack_entry.get("pack_digest")
        or pack["pack_digest"],
        "reviewed_head_sha": reviewed_head,
        "definition_digest": report["definition_digest"],
        "source_snapshot_digest": review_entry.get("source_snapshot_digest")
        or pack.get("source_snapshot_digest"),
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
    manifest["manifest_digest"] = _remediation_manifest_digest(manifest)
    return manifest


def remediation_start(
    root: Path,
    *,
    change_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    owner, owner_selection = _change_owner_root(root, change_id)
    state = StateStore(owner).load(change_id)
    latest = _latest_review_result(state)
    if not latest:
        raise IntegrityError("Remediation requires an imported ReviewIR")
    result_path, report = _read_review_result(owner, latest)
    current_head = git_head(owner)
    reviewed_head = report.get("head_sha")
    if not isinstance(reviewed_head, str) or not reviewed_head:
        raise IntegrityError("Latest ReviewIR has no reviewed HEAD")
    if (
        not current_head
        or run_git(
            owner,
            "merge-base",
            "--is-ancestor",
            reviewed_head,
            current_head,
            check=False,
        ).returncode
        != 0
    ):
        raise IntegrityError(
            "Latest ReviewIR is not an ancestor of the current remediation candidate: "
            f"{reviewed_head} -> {current_head}"
        )
    current_definition = current_definition_digest(owner, state)
    if report.get("definition_digest") != current_definition:
        raise IntegrityError("Latest ReviewIR definition digest is stale")
    current_approval = next(
        (
            item
            for item in reversed(derived_approval_statuses(owner, state))
            if item.get("decision") == "definition"
            and item.get("status") == "current"
            and item.get("object_digest") == current_definition
        ),
        None,
    )
    if state["control_level"] in {"standard", "critical"} and not current_approval:
        raise IntegrityError("Remediation requires a current definition approval")
    if git_source_dirty_paths(owner):
        raise IntegrityError("Remediation must start from a clean product source")
    open_findings = _review_actionable_findings(report)
    if not open_findings:
        return {
            "ok": True,
            "dry_run": dry_run,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "owner_root": str(owner),
            "owner_selection": owner_selection,
            "review_id": report["review_id"],
            "remediation_manifest_path": None,
            "remediation_manifest": None,
            "next_action": {
                "id": "no-remediation-required",
                "detail": result_path,
            },
        }
    relative_path = _existing_remediation_manifest_path(
        owner,
        change_id=change_id,
        review_entry=latest,
        review_id=report["review_id"],
    )
    if relative_path is None:
        projected = _canonical_remediation_manifest_path(
            change_id,
            report["review_id"],
        )
        return {
            "ok": False,
            "dry_run": dry_run,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "owner_root": str(owner),
            "owner_selection": owner_selection,
            "review_id": report["review_id"],
            "remediation_manifest_path": None,
            "remediation_manifest": None,
            "next_action": {
                "id": "recover-remediation-manifest",
                "detail": (
                    f"missing={projected}; reviewed_head={reviewed_head}; "
                    f"current_head={current_head}"
                ),
            },
        }
    relative_path, manifest = _load_remediation_manifest(
        owner,
        change_id=change_id,
        prior_review={
            "review_id": report["review_id"],
            "result_path": result_path,
            "result_digest": latest.get("result_digest")
            or _review_result_digest(report),
            "head_sha": reviewed_head,
            "definition_digest": report["definition_digest"],
            "remediation_manifest_path": latest.get("remediation_manifest_path"),
            "remediation_manifest_digest": latest.get(
                "remediation_manifest_digest"
            ),
        },
    )
    return {
        "ok": True,
        "dry_run": dry_run,
        "changed": False,
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "review_id": report["review_id"],
        "remediation_manifest_path": relative_path,
        "remediation_manifest": manifest,
        "next_action": {
            "id": "remediate-findings",
            "detail": relative_path,
        },
    }


def remediation_recover(
    root: Path,
    *,
    change_id: str,
    review_id: str | None,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    owner, owner_selection = _change_owner_root(root, change_id)
    state_store = StateStore(owner)
    state = state_store.load(change_id)
    latest = _latest_review_result(state)
    if latest is None:
        raise IntegrityError("Remediation recovery requires an imported ReviewIR")
    latest_review_id = latest.get("review_id")
    if not isinstance(latest_review_id, str) or not latest_review_id:
        raise IntegrityError("Latest review state entry has no review ID")
    if review_id is not None and review_id != latest_review_id:
        raise IntegrityError(
            "Remediation recovery is latest-only: "
            f"{review_id} != {latest_review_id}"
        )
    result_path, report = _read_review_result(owner, latest)
    if not _review_actionable_findings(report):
        return {
            "ok": True,
            "dry_run": dry_run,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "owner_root": str(owner),
            "owner_selection": owner_selection,
            "review_id": latest_review_id,
            "remediation_manifest_path": None,
            "remediation_manifest": None,
            "next_action": {
                "id": "no-remediation-required",
                "detail": result_path,
            },
        }
    linked_path = latest.get("remediation_manifest_path")
    if isinstance(linked_path, str) and linked_path:
        result = remediation_start(
            owner,
            change_id=change_id,
            dry_run=dry_run,
        )
        result["operation_id"] = operation_id
        return result
    pack_entry = next(
        (
            entry
            for entry in state["reviews"]
            if entry.get("kind") == "pack"
            and entry.get("review_id") == latest_review_id
        ),
        None,
    )
    if not isinstance(pack_entry, dict):
        raise IntegrityError(
            f"Remediation recovery cannot find ReviewPack {latest_review_id}"
        )
    pack_path = pack_entry.get("pack_path")
    if not isinstance(pack_path, str) or not pack_path:
        raise IntegrityError("ReviewPack state entry is missing pack_path")
    pack = read_json(safe_resolve(owner, pack_path, must_exist=True))
    _validate_review_pack(pack, change_id)
    if (
        pack_entry.get("pack_digest") != pack.get("pack_digest")
        or _review_pack_digest(pack) != pack.get("pack_digest")
    ):
        raise IntegrityError("ReviewPack digest does not match DLS state")
    _validate_review_report(report, change_id, pack)
    if (
        report.get("pack_digest") != pack.get("pack_digest")
        or report.get("head_sha") != pack.get("head_sha")
        or report.get("definition_digest") != pack.get("definition_digest")
    ):
        raise IntegrityError("ReviewIR is not bound to its canonical ReviewPack")
    reviewed_head = report["head_sha"]
    if (
        run_git(
            owner,
            "cat-file",
            "-e",
            f"{reviewed_head}^{{commit}}",
            check=False,
        ).returncode
        != 0
    ):
        raise IntegrityError(
            f"Reviewed Git commit is unavailable: {reviewed_head}"
        )
    current_head = git_head(owner)
    if (
        not current_head
        or run_git(
            owner,
            "merge-base",
            "--is-ancestor",
            reviewed_head,
            current_head,
            check=False,
        ).returncode
        != 0
    ):
        raise IntegrityError(
            "Current HEAD does not descend from the reviewed revision: "
            f"{reviewed_head} -> {current_head}"
        )
    if git_source_dirty_paths(owner):
        raise IntegrityError("Remediation recovery requires clean product source")
    current_definition = current_definition_digest(owner, state)
    if current_definition != report["definition_digest"]:
        raise IntegrityError("Remediation recovery definition digest is stale")
    current_approval = next(
        (
            item
            for item in reversed(derived_approval_statuses(owner, state))
            if item.get("decision") == "definition"
            and item.get("status") == "current"
            and item.get("object_digest") == current_definition
        ),
        None,
    )
    if state["control_level"] in {"standard", "critical"} and not current_approval:
        raise IntegrityError("Remediation recovery requires current definition approval")
    manifest_entry = {
        **latest,
        "result_digest": latest.get("result_digest")
        or _review_result_digest(report),
    }
    manifest = _build_remediation_manifest(
        owner,
        change_id=change_id,
        review_entry=manifest_entry,
        report=report,
        pack_entry=pack_entry,
        pack=pack,
        result_path=result_path,
        pack_path=pack_path,
        origin="legacy-recovery",
    )
    if manifest is None:
        raise IntegrityError("Remediation recovery produced no actionable findings")
    relative_path = _canonical_remediation_manifest_path(
        change_id,
        latest_review_id,
    )
    effective_operation_id = operation_id or str(uuid.uuid4())
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "owner_root": str(owner),
            "owner_selection": owner_selection,
            "review_id": latest_review_id,
            "reviewed_head": reviewed_head,
            "current_head": current_head,
            "remediation_manifest_path": None,
            "projected_remediation_manifest_path": relative_path,
            "remediation_manifest": manifest,
            "next_action": {
                "id": "write-recovered-remediation-manifest",
                "detail": relative_path,
            },
        }

    def mutate(value: dict[str, Any]) -> None:
        entry = next(
            (
                item
                for item in reversed(value["reviews"])
                if item.get("kind") == "result"
                and item.get("review_id") == latest_review_id
            ),
            None,
        )
        if entry is None:
            raise IntegrityError("Canonical ReviewIR disappeared during recovery")
        existing = entry.get("remediation_manifest_path")
        if existing not in {None, relative_path}:
            raise IntegrityError(
                f"ReviewIR already links another remediation manifest: {existing}"
            )
        entry["remediation_manifest_path"] = relative_path
        entry["remediation_manifest_digest"] = manifest["manifest_digest"]

    updated, changed = state_store.mutate_with_immutable_artifacts(
        change_id,
        expected_revision=state["state_revision"],
        operation_id=effective_operation_id,
        operation_kind="remediation-recover",
        artifacts=[(safe_resolve(owner, relative_path), manifest)],
        mutator=mutate,
    )
    return {
        "ok": True,
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "operation_id": effective_operation_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "review_id": latest_review_id,
        "reviewed_head": reviewed_head,
        "current_head": current_head,
        "remediation_manifest_path": relative_path,
        "remediation_manifest": manifest,
        "next_action": {
            "id": (
                "prepare-review"
                if current_head != reviewed_head
                else "remediate-findings"
            ),
            "detail": relative_path,
        },
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
    if (
        pack.get("runner_contract") == REVIEW_RUNNER_CONTRACT
        and pack["control_level"] == "routine"
        and pack["mode"] == "acceptance-grade"
    ):
        expected_lanes = ["native-diff", "semantic-dls"]
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
    decisions = pack.get("decisions")
    if decisions is not None:
        if not isinstance(decisions, dict) or set(decisions) != {"design", "architecture"}:
            raise IntegrityError("ReviewPack decisions projection is malformed")
        encoded_decisions = json.dumps(decisions, ensure_ascii=False).encode("utf-8")
        if len(encoded_decisions) > 4096:
            raise IntegrityError("ReviewPack decisions projection exceeds 4 KiB")
        forbidden = {"ref", "rationale", "content", "path", "git_blob"}
        if any(f'"{key}"' in encoded_decisions.decode("utf-8") for key in forbidden):
            raise IntegrityError("ReviewPack decisions projection contains private provenance")
    if schema_version == REVIEW_PACK_SCHEMA_VERSION:
        native_workspace_contract = pack.get("native_workspace_contract")
        if native_workspace_contract not in {None, NATIVE_WORKSPACE_CONTRACT}:
            raise IntegrityError(
                "Unsupported ReviewPack native workspace contract: "
                f"{native_workspace_contract}"
            )
        identifier_contract = pack.get("identifier_contract")
        if identifier_contract not in {None, REVIEW_IDENTIFIER_CONTRACT}:
            raise IntegrityError(
                f"Unsupported ReviewPack identifier contract: {identifier_contract}"
            )
        decision_repair_contract = pack.get("decision_repair_contract")
        if decision_repair_contract not in {
            None,
            REVIEW_DECISION_REPAIR_CONTRACT,
        }:
            raise IntegrityError(
                "Unsupported ReviewPack decision repair contract: "
                f"{decision_repair_contract}"
            )
        platform_profile = pack.get("platform_profile")
        if platform_profile is not None:
            if not isinstance(platform_profile, dict):
                raise IntegrityError("ReviewPack platform_profile must be an object")
            if set(platform_profile) != {"contract", "name", "digest"}:
                raise IntegrityError("ReviewPack platform_profile fields are invalid")
            if platform_profile.get("contract") != PROFILE_CONTRACT:
                raise IntegrityError("Unsupported ReviewPack platform profile contract")
            if not all(
                isinstance(platform_profile.get(field), str)
                and bool(platform_profile[field])
                for field in ("name", "digest")
            ):
                raise IntegrityError("ReviewPack platform profile identity is invalid")
        delivery_readiness = pack.get("delivery_readiness")
        if delivery_readiness is not None:
            if not isinstance(delivery_readiness, dict):
                raise IntegrityError("ReviewPack delivery_readiness must be an object")
            if set(delivery_readiness) != {
                "contract",
                "digest",
                "dependency_digest",
                "overlap_digest",
                "dependency_count",
                "exact_overlap_count",
            }:
                raise IntegrityError("ReviewPack delivery_readiness fields are invalid")
            if delivery_readiness.get("contract") != "dls-change-readiness/v1":
                raise IntegrityError("Unsupported ReviewPack delivery readiness contract")
            for field in ("digest", "dependency_digest", "overlap_digest"):
                if not isinstance(delivery_readiness.get(field), str):
                    raise IntegrityError("ReviewPack delivery readiness digest is invalid")
            for field in ("dependency_count", "exact_overlap_count"):
                if not isinstance(delivery_readiness.get(field), int):
                    raise IntegrityError("ReviewPack delivery readiness count is invalid")
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
            "remediation_manifest_path": entry.get(
                "remediation_manifest_path"
            ),
            "remediation_manifest_digest": entry.get(
                "remediation_manifest_digest"
            ),
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
    relative = _existing_remediation_manifest_path(
        root,
        change_id=change_id,
        review_entry=prior_review,
        review_id=prior_review["review_id"],
    )
    if relative is None:
        raise IntegrityError(
            "Missing canonical remediation manifest: "
            + _canonical_remediation_manifest_path(
                change_id,
                prior_review["review_id"],
            )
        )
    manifest = read_json(safe_resolve(root, relative))
    if manifest.get("schema_version") != 2:
        raise IntegrityError("Remediation manifest schema mismatch")
    digest = _remediation_manifest_digest(manifest)
    if manifest.get("manifest_digest") != digest:
        raise IntegrityError("Remediation manifest digest mismatch")
    recorded_digest = prior_review.get("remediation_manifest_digest")
    if isinstance(recorded_digest, str) and recorded_digest != digest:
        raise IntegrityError("Remediation manifest digest does not match DLS state")
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
    manifest_pack_path = manifest.get("review_pack_path")
    manifest_pack_digest = manifest.get("review_pack_digest")
    if manifest_pack_path is not None or manifest_pack_digest is not None:
        if (
            not isinstance(manifest_pack_path, str)
            or not isinstance(manifest_pack_digest, str)
        ):
            raise IntegrityError("Remediation manifest has incomplete ReviewPack provenance")
        pack = read_json(
            safe_resolve(root, manifest_pack_path, must_exist=True)
        )
        if (
            pack.get("review_id") != prior_review["review_id"]
            or pack.get("pack_digest") != manifest_pack_digest
            or _review_pack_digest(pack) != manifest_pack_digest
        ):
            raise IntegrityError("Remediation manifest ReviewPack provenance mismatch")
    for item in manifest.get("inputs", []):
        if not isinstance(item, dict):
            raise IntegrityError("Remediation manifest has an invalid input record")
        input_path = item.get("path")
        reason = item.get("reason")
        if not isinstance(input_path, str):
            raise IntegrityError("Remediation manifest input has no path")
        if reason == "reviewed-authored-artifact":
            input_payload = _git_blob_bytes(
                root,
                prior_review["head_sha"],
                input_path,
            )
        elif reason is None:
            # Historical v2 manifests predate reason-tagged inputs.
            continue
        else:
            input_payload = safe_resolve(
                root,
                input_path,
                must_exist=True,
            ).read_bytes()
        if sha256_bytes(input_payload) != item.get("sha256"):
            raise IntegrityError(
                f"Remediation manifest input digest mismatch: {input_path}"
            )
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


def _review_pack_state_entry(
    pack: dict[str, Any],
    relative_path: str,
) -> dict[str, Any]:
    return {
        "review_id": pack["review_id"],
        "kind": "pack",
        "runner_contract": pack.get("runner_contract"),
        "context_contract": pack.get("context_contract"),
        "economy_contract": pack.get("economy_contract"),
        "budget_contract": pack.get("budget_contract"),
        "native_output_contract": pack.get("native_output_contract"),
        "native_workspace_contract": pack.get("native_workspace_contract"),
        "identifier_contract": pack.get("identifier_contract"),
        "decision_repair_contract": pack.get("decision_repair_contract"),
        "decisions": pack.get("decisions"),
        "pack_path": relative_path,
        "base_sha": pack["base_sha"],
        "comparison_base_sha": pack["comparison_base_sha"],
        "head_sha": pack["head_sha"],
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
    _state_override: dict[str, Any] | None = None,
    _skip_review_gate: bool = False,
) -> dict[str, Any]:
    if not is_git_repo(root):
        raise IntegrityError("Review pack requires Git")
    state_store = StateStore(root)
    stored_state = state_store.load(change_id)
    state = _state_override or stored_state
    from .parallel_delivery import change_readiness

    delivery_readiness = change_readiness(
        root,
        change_id=change_id,
        stage="review",
        include_overlap=True,
    )
    if not delivery_readiness["ready"]:
        action = delivery_readiness["next_action"] or {
            "id": "wait-dependency",
            "detail": "delivery dependency is not satisfied",
        }
        raise IntegrityError(
            f"ReviewPack dependency gate failed: {action['id']}: {action['detail']}"
        )
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
    existing_operation = _operation(stored_state, effective_operation_id)
    if existing_operation:
        _require_operation_kind(existing_operation, operation_kind)
        pack = read_json(safe_resolve(root, relative_path, must_exist=True))
        return {
            "ok": True,
            "dry_run": False,
            "changed": False,
            "change_id": change_id,
            "state_revision": stored_state["state_revision"],
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
        gate = (
            {"ok": True, "checks": []}
            if _skip_review_gate
            else check(root, change_id=change_id, gate="review")
        )
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
    platform_profile = resolve_profile(root, config=load_config(root))
    pack_approvals = derived_approval_statuses(root, state)
    decision_gate = decision_readiness(
        root,
        state,
        pack_approvals,
        require_definition=state["control_level"] in {"standard", "critical"},
    )
    if not decision_gate["ready"]:
        action = decision_gate["next_action"]
        assert action is not None
        raise IntegrityError(
            f"ReviewPack decision gate failed: {action['id']}: {action['detail']}"
        )
    pack = {
        "schema_version": REVIEW_PACK_SCHEMA_VERSION,
        "runner_contract": REVIEW_RUNNER_CONTRACT,
        "context_contract": REVIEW_CONTEXT_CONTRACT,
        "economy_contract": REVIEW_ECONOMY_CONTRACT,
        "budget_contract": REVIEW_BUDGET_CONTRACT,
        "command_event_contract": COMMAND_EVENT_CONTRACT,
        "native_output_contract": NATIVE_OUTPUT_CONTRACT,
        "native_workspace_contract": NATIVE_WORKSPACE_CONTRACT,
        "identifier_contract": REVIEW_IDENTIFIER_CONTRACT,
        "decision_repair_contract": REVIEW_DECISION_REPAIR_CONTRACT,
        "platform_profile": {
            "contract": platform_profile["contract"],
            "name": platform_profile["name"],
            "digest": platform_profile["digest"],
        },
        "decisions": decision_gate["decisions"],
        "delivery_readiness": {
            "contract": delivery_readiness["contract"],
            "digest": delivery_readiness["digest"],
            "dependency_digest": delivery_readiness["dependencies"]["digest"],
            "overlap_digest": delivery_readiness["overlap"]["digest"],
            "dependency_count": len(delivery_readiness["dependencies"]["items"]),
            "exact_overlap_count": delivery_readiness["overlap"]["exact_overlap_count"],
        },
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
        "required_lanes": (
            ["native-diff", "semantic-dls"]
            if state["control_level"] == "routine" and mode == "acceptance-grade"
            else _review_pack_required_lanes(
                control_level=state["control_level"],
                mode=mode,
            )
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
        value["reviews"].append(_review_pack_state_entry(pack, relative_path))
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
    base_ref: str | None,
    expected_revision: int,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    root, owner_selection = _change_owner_root(root, change_id)
    if not is_git_repo(root):
        raise IntegrityError("Review readiness requires Git")
    state = StateStore(root).load(change_id)
    _require_revision(state, expected_revision)
    review_decision_gate = decision_readiness(
        root,
        state,
        derived_approval_statuses(root, state),
        require_definition=state["control_level"] in {"standard", "critical"},
    )
    if not review_decision_gate["ready"]:
        action = review_decision_gate["next_action"]
        assert action is not None
        return _review_ready_blocked(
            change_id=change_id,
            state_revision=state["state_revision"],
            next_action=action["id"],
            detail=action["detail"],
            dry_run=dry_run,
        )
    from .parallel_delivery import change_readiness

    delivery_readiness = change_readiness(
        root,
        change_id=change_id,
        stage="review",
        include_overlap=True,
    )
    if not delivery_readiness["ready"]:
        action = delivery_readiness["next_action"] or {
            "id": "wait-dependency",
            "detail": "delivery dependency is not satisfied",
        }
        return _review_ready_blocked(
            change_id=change_id,
            state_revision=state["state_revision"],
            next_action=action["id"],
            detail=action["detail"],
            dry_run=dry_run,
        )
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
    effective_base_ref = base_ref
    remediation_manifest: tuple[str, dict[str, Any]] | None = None
    current_head = git_head(root)
    if prior_review is not None and prior_report is not None:
        review_mode = "remediation"
        effective_base_ref = (
            base_ref
            or prior_report.get("epic_base_sha")
            or prior_report.get("base_sha")
        )
        manifest_path = _existing_remediation_manifest_path(
            root,
            change_id=change_id,
            review_entry=prior_review,
            review_id=prior_review["review_id"],
        )
        if manifest_path is None:
            projected = _canonical_remediation_manifest_path(
                change_id,
                prior_review["review_id"],
            )
            return _review_ready_blocked(
                change_id=change_id,
                state_revision=state["state_revision"],
                next_action="recover-remediation-manifest",
                detail=(
                    f"missing={projected}; reviewed_head={prior_review['head_sha']}; "
                    f"current_head={current_head}"
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
    if not isinstance(effective_base_ref, str) or not effective_base_ref:
        return _review_ready_blocked(
            change_id=change_id,
            state_revision=state["state_revision"],
            next_action="provide-review-base",
            detail="first review requires --base BASE",
            dry_run=dry_run,
        )
    comparison_ref = (
        prior_review["head_sha"]
        if prior_review is not None and prior_report is not None
        else effective_base_ref
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
            if status not in {"addressed", "note"} or not _disposition_applies_to_head(
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
        base_ref=effective_base_ref,
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
    result["owner_root"] = str(root)
    result["owner_selection"] = owner_selection
    result["handoff_required"] = True
    result["next_action"] = {
        "id": "open-review-task",
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
    allow_missing_current: bool = False,
) -> tuple[
    Path,
    dict[str, Any],
    dict[str, Any] | None,
    str | None,
    str,
]:
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
        owner = resolve_change_root(root, change_id)
        if owner != root:
            owner_selection = "registered-worktree"
        state = StateStore(owner).load(change_id)
        completed_review_ids = {
            entry.get("review_id")
            for entry in state["reviews"]
            if entry.get("kind") == "result"
        }
        current_head = git_head(owner)
        pack_entry = next(
            (
                entry
                for entry in reversed(state["reviews"])
                if entry.get("kind") == "pack"
                and entry.get("review_id") not in completed_review_ids
                and entry.get("head_sha") == current_head
                and isinstance(entry.get("pack_path"), str)
            ),
            None,
        )
        if not pack_entry:
            if allow_missing_current:
                return owner, state, None, None, owner_selection
            raise IntegrityError(
                f"No unfinished ReviewPack for current HEAD {current_head} "
                f"and {change_id} in {owner}; "
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
    pack_decisions = pack.get("decisions")
    current_decisions = decision_projection(
        root,
        state,
        derived_approval_statuses(root, state),
    )
    if not review_pack_decisions_current(pack_decisions, current_decisions):
        raise IntegrityError("ReviewPack design or architecture decision is stale")
    pack_profile = pack.get("platform_profile")
    if isinstance(pack_profile, dict):
        current_profile = resolve_profile(root, config=load_config(root))
        if pack_profile.get("digest") != current_profile["digest"]:
            raise IntegrityError("ReviewPack platform profile is stale")
    pack_readiness = pack.get("delivery_readiness")
    if isinstance(pack_readiness, dict):
        from .parallel_delivery import change_readiness

        current_readiness = change_readiness(
            root,
            change_id=state["change_id"],
            stage="review",
            include_overlap=True,
        )
        if not current_readiness["ready"]:
            action = current_readiness["next_action"] or {
                "id": "wait-dependency",
                "detail": "delivery dependency is not satisfied",
            }
            raise IntegrityError(
                f"ReviewPack delivery dependency is blocked: {action['id']}: {action['detail']}"
            )
        if pack_readiness.get("digest") != current_readiness["digest"]:
            raise IntegrityError("ReviewPack dependency or overlap snapshot is stale")
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


def _routine_review_prompt(pack: dict[str, Any]) -> str:
    prompt = (PLUGIN_ROOT / "assets/review-prompts/routine.md").read_text(
        encoding="utf-8"
    )
    values = {
        "CHANGE_ID": pack["change_id"],
        "HEAD_SHA": pack["head_sha"],
        "COMPARISON_BASE_SHA": pack.get(
            "comparison_base_sha", pack["merge_base"]
        ),
        "CANONICAL_TICKET_IDS": json.dumps(
            list(pack.get("tickets", {})), ensure_ascii=False
        ),
        "REQUIRED_PRIOR_FINDING_IDS": json.dumps(
            [
                item["finding_id"]
                for item in pack.get("required_prior_findings", [])
            ],
            ensure_ascii=False,
        ),
        "CANONICAL_PRIOR_FINDINGS": json.dumps(
            pack.get("required_prior_findings", []),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    for key, value in values.items():
        prompt = prompt.replace(f"{{{{{key}}}}}", value)
    if "{{" in prompt or "}}" in prompt:
        raise IntegrityError("Routine review prompt has unresolved placeholders")
    return prompt


def _routine_plaintext_decision(
    text: str,
    *,
    pack: dict[str, Any],
) -> dict[str, Any]:
    """Convert Codex review's documented presentation into a DLS decision.

    Some Codex CLI builds accept `--output-schema` on `exec review` but still
    render the built-in human review presentation. DLS preserves that raw
    output and derives this deliberately small, auditable projection without a
    second model call.
    """

    stripped = text.strip()
    clear_match = re.match(
        r"^(?:review-clear|no findings(?: found)?)\s*[:.\-—]?\s*(.*)$",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    prior_findings = {
        item["finding_id"]: item
        for item in pack.get("required_prior_findings", [])
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str)
    }
    if clear_match:
        summary = clear_match.group(1).strip() or "No review findings."
        return {
            "verdict": "review-clear",
            "summary": summary,
            "findings": [],
            "prior_finding_verdicts": [
                {
                    "finding_id": finding_id,
                    "verdict": "verified",
                    "replacement_finding_id": None,
                    "evidence": [
                        "The independent routine review reported no remaining finding."
                    ],
                }
                for finding_id in prior_findings
            ],
        }
    blocked_match = re.match(
        r"^blocked\s*[:.\-—]?\s*(.*)$",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if blocked_match and "\n- [P" not in stripped:
        return {
            "verdict": "blocked",
            "summary": blocked_match.group(1).strip() or "Review was blocked.",
            "findings": [],
            "prior_finding_verdicts": [],
        }

    lines = stripped.splitlines()
    comment_starts: list[tuple[int, re.Match[str]]] = []
    comment_pattern = re.compile(
        r"^-\s+\[P([0-3])\]\s+(.+?)\s+(?:—|-)\s+(.+?)\s*$"
    )
    for index, line in enumerate(lines):
        if match := comment_pattern.match(line):
            comment_starts.append((index, match))
    if not comment_starts:
        unsafe_markers = (
            "unable to review",
            "could not review",
            "review was blocked",
            "review is blocked",
            "not reviewed",
        )
        if any(marker in stripped.casefold() for marker in unsafe_markers):
            raise IntegrityError("routine review plaintext reports an incomplete review")
        # The built-in Codex review presentation emits actionable findings as
        # P0-P3 review comments. A non-empty successful final message with no
        # such comments is its native clean result.
        return {
            "verdict": "review-clear",
            "summary": stripped,
            "findings": [],
            "prior_finding_verdicts": [
                {
                    "finding_id": finding_id,
                    "verdict": "verified",
                    "replacement_finding_id": None,
                    "evidence": [
                        "The independent routine review emitted no review comment."
                    ],
                }
                for finding_id in prior_findings
            ],
        }

    used_ids = set(prior_findings)
    findings: list[dict[str, Any]] = []
    replacements: dict[str, str] = {}
    for ordinal, (line_index, match) in enumerate(comment_starts, start=1):
        next_index = (
            comment_starts[ordinal][0]
            if ordinal < len(comment_starts)
            else len(lines)
        )
        priority, title, location = match.groups()
        body = " ".join(
            item.strip()
            for item in lines[line_index + 1 : next_index]
            if item.strip()
        )
        prior_id: str | None = None
        if prior_match := re.match(r"^\[PRIOR:([^\]]+)\]\s*(.*)$", title):
            prior_id = prior_match.group(1)
            title = prior_match.group(2).strip()
            if prior_id not in prior_findings:
                raise IntegrityError(
                    f"routine review references unknown prior finding: {prior_id}"
                )
            if prior_id in replacements:
                raise IntegrityError(
                    f"routine review repeats prior finding: {prior_id}"
                )
        if "/checkout/" in location:
            location = location.split("/checkout/", 1)[1]
        location = location.removeprefix("./")
        digest = hashlib.sha256(
            (
                f"{pack['review_id']}\n{ordinal}\n{title}\n{location}\n{body}"
            ).encode("utf-8")
        ).hexdigest()[:10].upper()
        finding_id = f"{pack['change_id'].replace('-', '')}-R{digest}"
        if finding_id in used_ids:
            raise IntegrityError("routine review produced a duplicate derived finding ID")
        used_ids.add(finding_id)
        if prior_id is not None:
            replacements[prior_id] = finding_id
        findings.append(
            {
                "id": finding_id,
                "severity": "blocker" if priority in {"0", "1"} else "should-fix",
                "kind": "defect",
                "location": location,
                "issue": title,
                "impact": body or title,
                "required_fix": body or "Address the reported review issue.",
                "ticket_ids": [],
                "requirement_ids": [],
                "blocks": ["review", "acceptance"],
                "provenance": ["codex-exec-review"],
            }
        )
    first_comment = comment_starts[0][0]
    summary = " ".join(item.strip() for item in lines[:first_comment] if item.strip())
    summary = re.sub(r"^not-clear\s*[:.\-—]?\s*", "", summary, flags=re.IGNORECASE)
    return {
        "verdict": "not-clear",
        "summary": summary or f"Routine review reported {len(findings)} finding(s).",
        "findings": findings,
        "prior_finding_verdicts": [
            {
                "finding_id": finding_id,
                "verdict": "still-open" if finding_id in replacements else "verified",
                "replacement_finding_id": replacements.get(finding_id),
                "evidence": [
                    (
                        "The routine review reported a replacement finding."
                        if finding_id in replacements
                        else "The routine review did not report the prior finding as remaining."
                    )
                ],
            }
            for finding_id in prior_findings
        ],
    }


NATIVE_PLAINTEXT_PROJECTION_CONTRACT = "dls-native-plaintext/v1"


def _native_plaintext_projection(text: str) -> dict[str, Any]:
    """Project Codex' built-in review presentation into native-review/v2.

    `codex exec review` may accept `--output-schema` while still writing its
    human-facing presentation to `--output-last-message`.  This parser is
    deliberately conservative: it accepts explicit clean markers or complete
    P0-P3 review comments only.  The raw model output remains the provenance
    artifact; this projection is DLS-owned and independently digest-bound.
    """

    stripped = text.strip()
    if not stripped:
        raise IntegrityError("native review plaintext is empty")
    clean_match = re.match(
        r"^(?:review[- ]clear|no findings(?: found)?|no actionable findings)"
        r"\s*[:.\-—]?\s*(.*)$",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if clean_match:
        summary = clean_match.group(1).strip() or "No actionable review findings."
        return {"summary": summary, "findings": []}
    clean_sentence = re.search(
        r"(?:^|(?<=[.!?])\s+)"
        r"no actionable (?:review )?(?:findings|regressions) "
        r"(?:were )?(?:identified|found)"
        r"(?: in (?:the )?(?:reviewed )?(?:diff|changes|candidate))?"
        r"(?:[.!?](?:\s|$)|$)",
        stripped,
        flags=re.IGNORECASE,
    )
    if clean_sentence:
        return {"summary": stripped, "findings": []}

    lines = stripped.splitlines()
    comment_pattern = re.compile(
        r"^-\s+\[P([0-3])\]\s+(.+?)\s+(?:—|-)\s+"
        r"(.+?:[0-9]+(?:-[0-9]+)?)\s*$"
    )
    comment_starts: list[tuple[int, re.Match[str]]] = []
    for index, line in enumerate(lines):
        if match := comment_pattern.match(line):
            comment_starts.append((index, match))
        elif re.match(r"^-\s+\[P[0-3]\]", line):
            raise IntegrityError(
                "native review plaintext contains an unparseable review comment"
            )
    if not comment_starts:
        raise IntegrityError(
            "native review plaintext has neither an explicit clean marker nor "
            "a complete P0-P3 review comment"
        )

    findings: list[dict[str, Any]] = []
    for ordinal, (line_index, match) in enumerate(comment_starts):
        next_index = (
            comment_starts[ordinal + 1][0]
            if ordinal + 1 < len(comment_starts)
            else len(lines)
        )
        priority, title, location = match.groups()
        body_lines = [
            item.strip()
            for item in lines[line_index + 1 : next_index]
            if item.strip()
        ]
        body = " ".join(body_lines)
        if not body:
            raise IntegrityError(
                "native review plaintext comment is missing its explanation"
            )
        if "/checkout/" in location:
            location = location.split("/checkout/", 1)[1]
        location = location.removeprefix("./")
        severity = {
            "0": "blocker",
            "1": "blocker",
            "2": "should-fix",
            "3": "note",
        }[priority]
        findings.append(
            {
                "severity": severity,
                "location": location,
                "issue": title.strip(),
                "impact": body,
                "required_fix": body,
            }
        )

    first_comment = comment_starts[0][0]
    summary_parts = [
        item.strip()
        for item in lines[:first_comment]
        if item.strip() and item.strip().casefold() != "review comment:"
    ]
    return {
        "summary": " ".join(summary_parts)
        or f"Native review reported {len(findings)} finding(s).",
        "findings": findings,
    }


def _native_indeterminate_plaintext_projection(
    text: str,
    *,
    transcript_text: str,
) -> dict[str, Any]:
    """Preserve an unstructured native conclusion without calling it clean.

    ``codex exec review`` can complete successfully while writing a
    human-facing final message even when ``--output-schema`` was supplied. For
    standard and critical reviews, DLS may retain that message as an
    indeterminate native projection only when the immutable JSONL transcript
    proves it is the completed turn's final agent message. The runner then
    forces independent semantic reconciliation; this projection is never a
    clean native verdict by itself.
    """

    stripped = text.strip()
    if not stripped:
        raise IntegrityError("native review plaintext is empty")
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(transcript_text.splitlines(), start=1):
        candidate = line.strip()
        if not candidate.startswith("{"):
            continue
        try:
            event = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise IntegrityError(
                "native transcript contains malformed JSONL at "
                f"line {line_number}: {exc.msg}"
            ) from exc
        if not isinstance(event, dict):
            raise IntegrityError("native transcript event must be an object")
        events.append(event)
    if not events:
        raise IntegrityError("native transcript contains no JSONL events")
    if any(event.get("type") in {"turn.failed", "error"} for event in events):
        raise IntegrityError("native transcript contains a failed turn")
    messages = [
        (index, event["item"]["text"])
        for index, event in enumerate(events)
        if event.get("type") == "item.completed"
        and isinstance(event.get("item"), dict)
        and event["item"].get("type") == "agent_message"
        and isinstance(event["item"].get("text"), str)
    ]
    if not messages:
        raise IntegrityError("native transcript has no completed agent message")
    final_message_index, final_message = messages[-1]
    if final_message.strip() != stripped:
        raise IntegrityError(
            "native output-last-message does not match the transcript's final agent message"
        )
    completed_turns = [
        index
        for index, event in enumerate(events)
        if event.get("type") == "turn.completed"
    ]
    if not completed_turns or completed_turns[-1] <= final_message_index:
        raise IntegrityError("native transcript does not complete after its final message")
    return {"summary": stripped, "findings": []}


def _native_review_argv(
    pack: dict[str, Any],
    final_output_path: str,
    *,
    working_directory: str | None = None,
) -> list[str]:
    routine = pack.get("control_level") == "routine"
    schema_path = (
        SCHEMAS_ROOT / "review-decision.schema.json"
        if routine
        else SCHEMAS_ROOT / "native-review.schema.json"
    )
    argv = [
        "codex",
        "exec",
        "--strict-config",
        "--model",
        NATIVE_REVIEW_MODEL,
        "-c",
        f'model_reasoning_effort="{NATIVE_REVIEW_REASONING_EFFORT}"',
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--json",
        "--color",
        "never",
    ]
    if working_directory is not None:
        argv.extend(["--cd", working_directory])
    if pack.get("native_output_contract") == NATIVE_OUTPUT_CONTRACT:
        argv.extend(
            [
                "--output-schema",
                str(schema_path),
            ]
        )
    argv.extend(["--output-last-message", final_output_path, "review"])
    if routine:
        # The official review CLI treats a custom prompt as one complete review
        # target and rejects combining it with `--base`. The DLS-owned prompt
        # binds the immutable base/head SHAs, while review-start independently
        # verifies both revisions before and after the isolated model call.
        argv.append(_routine_review_prompt(pack))
    else:
        argv.extend(
            [
                "--base",
                pack.get("comparison_base_sha", pack["merge_base"]),
            ]
        )
    return argv


def _create_native_review_workspace(
    owner: Path,
    *,
    head_sha: str,
    parent: Path,
) -> Path:
    """Create a standalone exact-HEAD clone for the built-in review command.

    A linked Git worktree shares repository metadata with its owner.  Some
    Codex CLI builds can then resolve the owner checkout and include its dirty
    generated `.dls` sidecar in `review --base`.  A no-hardlinks clone with no
    remote or alternates gives the model one clean repository root only.
    """

    workspace = parent / "checkout"
    run_git(
        owner,
        "-c",
        "gc.auto=0",
        "-c",
        "gc.autoDetach=false",
        "-c",
        "maintenance.auto=false",
        "clone",
        "--no-hardlinks",
        "--no-checkout",
        "--quiet",
        "--",
        str(owner.resolve()),
        str(workspace),
    )
    run_git(workspace, "checkout", "--detach", "--quiet", head_sha)
    run_git(workspace, "remote", "remove", "origin", check=False)
    if git_head(workspace) != head_sha:
        raise IntegrityError("Native review workspace is not at the exact ReviewPack HEAD")
    if git_source_dirty_paths(workspace):
        raise IntegrityError("Native review workspace is not clean")
    common_dir_raw = run_git(workspace, "rev-parse", "--git-common-dir").stdout.strip()
    common_dir = Path(common_dir_raw)
    if not common_dir.is_absolute():
        common_dir = workspace / common_dir
    expected_git_dir = workspace / ".git"
    try:
        common_dir.resolve().relative_to(expected_git_dir.resolve())
    except ValueError as exc:
        raise IntegrityError(
            "Native review workspace still shares Git metadata with its owner"
        ) from exc
    alternates = expected_git_dir / "objects" / "info" / "alternates"
    if alternates.exists():
        raise IntegrityError("Native review workspace must not use Git alternates")
    config_path = expected_git_dir / "config"
    if owner.resolve().as_posix() in config_path.read_text(encoding="utf-8"):
        raise IntegrityError("Native review workspace leaks its owner path")
    return workspace


def _successful_native_entry(
    root: Path,
    *,
    state: dict[str, Any],
    review_id: str,
    pack: dict[str, Any],
) -> dict[str, Any] | None:
    entry: dict[str, Any] | None = None
    for candidate in reversed(state["reviews"]):
        if (
            candidate.get("kind") != "native"
            or candidate.get("review_id") != review_id
            or candidate.get("status") != "completed"
        ):
            continue
        if (
            pack.get("runner_contract") == REVIEW_RUNNER_CONTRACT
            and candidate.get("lane_key") != "native"
        ):
            continue
        workspace_contract = pack.get("native_workspace_contract")
        if workspace_contract is not None and (
            candidate.get("native_workspace_contract") != workspace_contract
            or candidate.get("workspace_isolation")
            != NATIVE_WORKSPACE_ISOLATION
            or candidate.get("workspace_head_sha") != pack.get("head_sha")
        ):
            continue
        entry = candidate
        break
    if entry is None:
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
    decision_status = entry.get("native_decision_status")
    if decision_status not in {None, "determinate", "indeterminate"}:
        raise IntegrityError("Native review decision status is invalid")
    normalized_path = entry.get("normalized_output_path")
    if isinstance(normalized_path, str):
        normalized = safe_resolve(root, normalized_path)
        if not normalized.is_file():
            raise IntegrityError("Native normalized output cache is missing")
        if sha256_file(normalized) != entry.get("normalized_output_digest"):
            raise IntegrityError("Native normalized output digest mismatch")
    if decision_status == "indeterminate":
        if (
            entry.get("native_output_format")
            != "codex-review-plaintext-indeterminate"
            or entry.get("native_plaintext_projection_contract")
            != NATIVE_INDETERMINATE_PROJECTION_CONTRACT
            or entry.get("native_transcript_validation_contract")
            != NATIVE_TRANSCRIPT_VALIDATION_CONTRACT
            or not isinstance(normalized_path, str)
        ):
            raise IntegrityError(
                "Indeterminate native review provenance is incomplete"
            )
    return entry


def _recover_native_plaintext_projection(
    root: Path,
    *,
    state_store: StateStore,
    change_id: str,
    pack: dict[str, Any],
    entry: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    output_relative = entry.get("output_path")
    attempt_id = entry.get("attempt_id")
    if not isinstance(output_relative, str) or not isinstance(attempt_id, str):
        raise IntegrityError("Native plaintext recovery metadata is incomplete")

    def record_failure(kind: str, error: Exception) -> None:
        state_store.finish_review_lane(
            change_id,
            attempt_id=attempt_id,
            expected_status="invalid-output",
            updates={
                "status": "invalid-output",
                "native_recovery_status": kind,
                "native_recovery_error": redact_text(str(error)),
            },
        )

    prior_recovery = entry.get("native_recovery_status")
    if prior_recovery in {"unsafe", "integrity-failed"}:
        raise IntegrityError(
            entry.get("native_recovery_error")
            or f"Native plaintext recovery is {prior_recovery}"
        )
    output_path = safe_resolve(root, output_relative, must_exist=True)
    if sha256_file(output_path) != entry.get("output_digest"):
        error = IntegrityError("Native plaintext recovery raw output digest mismatch")
        record_failure("integrity-failed", error)
        raise error
    raw_text = output_path.read_text(encoding="utf-8")
    output_format = "codex-review-plaintext"
    projection_contract = NATIVE_PLAINTEXT_PROJECTION_CONTRACT
    decision_status = "determinate"
    transcript_validation_contract: str | None = None
    try:
        decision = (
            _routine_plaintext_decision(raw_text, pack=pack)
            if pack.get("control_level") == "routine"
            else _native_plaintext_projection(raw_text)
        )
    except IntegrityError as strict_error:
        if pack.get("control_level") not in {"standard", "critical"}:
            record_failure("unsafe", strict_error)
            raise
        transcript_relative = entry.get("transcript_path")
        if not isinstance(transcript_relative, str):
            error = IntegrityError(
                "Native indeterminate recovery requires a diagnostic transcript"
            )
            record_failure("integrity-failed", error)
            raise error from strict_error
        try:
            transcript_path = safe_resolve(root, transcript_relative, must_exist=True)
            if sha256_file(transcript_path) != entry.get("transcript_digest"):
                raise IntegrityError(
                    "Native indeterminate recovery transcript digest mismatch"
                )
        except IntegrityError as error:
            record_failure("integrity-failed", error)
            raise error from strict_error
        try:
            decision = _native_indeterminate_plaintext_projection(
                raw_text,
                transcript_text=transcript_path.read_text(encoding="utf-8"),
            )
        except (OSError, UnicodeError, IntegrityError) as recovery_error:
            error = IntegrityError(
                "Native plaintext is neither strictly projectable nor safely "
                f"indeterminate: {recovery_error}"
            )
            record_failure("unsafe", error)
            raise error from strict_error
        output_format = "codex-review-plaintext-indeterminate"
        projection_contract = NATIVE_INDETERMINATE_PROJECTION_CONTRACT
        decision_status = "indeterminate"
        transcript_validation_contract = NATIVE_TRANSCRIPT_VALIDATION_CONTRACT
    normalized_relative = (
        f".dls/cache/reviews/{change_id}/{pack['review_id']}/"
        f"native-normalized-{attempt_id}.json"
    )
    normalized_path = safe_resolve(root, normalized_relative)
    atomic_write_json(normalized_path, decision, backup=False)
    try:
        updated, recovered, changed = state_store.finish_review_lane(
            change_id,
            attempt_id=attempt_id,
            expected_status="invalid-output",
            updates={
                "status": "completed",
                "normalized_output_path": normalized_relative,
                "normalized_output_digest": sha256_file(normalized_path),
                "native_output_format": output_format,
                "native_decision_status": decision_status,
                "native_plaintext_projection_contract": projection_contract,
                "native_transcript_validation_contract": (
                    transcript_validation_contract
                ),
                "native_recovery_status": "recovered",
                "native_recovery_error": None,
                "failure_reason": None,
                "completed_at": utc_now(),
            },
        )
    except Exception:
        normalized_path.unlink(missing_ok=True)
        raise
    return updated, recovered, changed


def _semantic_review_effort(state: dict[str, Any]) -> str:
    high_risk = {"concurrency", "security-privacy", "auth"}
    if state["control_level"] == "critical" and high_risk.intersection(
        state["impact_tags"]
    ):
        return "xhigh"
    return "high"


def _review_lane_entries(
    state: dict[str, Any],
    *,
    review_id: str,
    lane_key: str,
) -> list[dict[str, Any]]:
    return [
        item
        for item in state["reviews"]
        if isinstance(item, dict)
        and item.get("review_id") == review_id
        and item.get("lane_key") == lane_key
    ]


def _process_is_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _attempt_lease_expired(attempt: dict[str, Any]) -> bool:
    started_at = attempt.get("started_at")
    if not isinstance(started_at, str):
        return True
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return elapsed > NATIVE_REVIEW_TIMEOUT_SECONDS + 60


def _recover_legacy_double_counted_budget_attempt(
    root: Path,
    *,
    state_store: StateStore,
    change_id: str,
    attempt: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Reclassify the v0.5.0 start/completion double-count failure once."""
    budget = attempt.get("budget")
    transcript_relative = attempt.get("transcript_path")
    if (
        attempt.get("status") != "budget-exceeded"
        or attempt.get("command_event_contract") is not None
        or not isinstance(budget, dict)
        or not isinstance(budget.get("command_events"), int)
        or not isinstance(attempt.get("command_events"), int)
        or attempt["command_events"] <= budget["command_events"]
        or int(attempt.get("attempt_ordinal", 0)) >= REVIEW_LANE_MAX_ATTEMPTS
        or attempt.get("timed_out") is True
        or attempt.get("overflow") is True
        or attempt.get("transcript_truncated") is True
        or not isinstance(transcript_relative, str)
        or not isinstance(attempt.get("transcript_digest"), str)
    ):
        return state_store.load(change_id), False
    transcript_path = safe_resolve(root, transcript_relative, must_exist=True)
    if sha256_file(transcript_path) != attempt["transcript_digest"]:
        raise IntegrityError("Legacy budget transcript digest mismatch")
    transcript_bytes = transcript_path.read_bytes()
    if (
        isinstance(budget.get("transcript_bytes"), int)
        and len(transcript_bytes) > budget["transcript_bytes"]
    ):
        return state_store.load(change_id), False
    usage = _codex_usage_from_output(transcript_bytes)
    if usage is not None:
        usage_total = processed_tokens(usage)
        if any(
            isinstance(budget.get(key), int) and usage_total > budget[key]
            for key in ("lane_tokens", "aggregate_tokens")
        ):
            return state_store.load(change_id), False
    logical_count = _logical_command_event_count(transcript_bytes)
    if logical_count > budget["command_events"] or logical_count >= attempt["command_events"]:
        return state_store.load(change_id), False
    updated, _, changed = state_store.finish_review_lane(
        change_id,
        attempt_id=attempt["attempt_id"],
        expected_status="budget-exceeded",
        updates={
            "status": "abandoned",
            "legacy_budget_reclassified": True,
            "logical_command_events": logical_count,
            "command_event_contract": "legacy-double-count/v0",
            "failure_reason": (
                "v0.5.0 counted paired command start/completion events twice; "
                f"recorded={attempt['command_events']}, logical={logical_count}"
            ),
            "budget_recovery": {
                "kind": "paired-command-events",
                "recorded_command_events": attempt["command_events"],
                "logical_command_events": logical_count,
                "budget": budget["command_events"],
                "reclassified_at": utc_now(),
            },
        },
    )
    return updated, changed


def _lane_wait_response(
    *,
    change_id: str,
    state: dict[str, Any],
    operation_id: str,
    owner: Path,
    owner_selection: str,
    pack: dict[str, Any],
    relative_pack_path: str | None,
    pack_created: bool,
    attempt: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "dry_run": False,
        "changed": False,
        "status": "running",
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "operation_id": operation_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "review_id": pack["review_id"],
        "review_pack_path": relative_pack_path,
        "pack_created": pack_created,
        "review_mode": pack.get("review_mode", "full"),
        "control_level": pack.get("control_level"),
        "risk_lenses": pack.get("risk_lenses", []),
        "required_prior_findings": pack.get("required_prior_findings", []),
        "native_required": True,
        "native_reused": False,
        "native_argv": attempt.get("argv"),
        "native": attempt,
        "native_coverage": list(pack.get("prior_native_coverage", [])),
        "review_context_path": None,
        "review_context_digest": None,
        "semantic_model": (
            NATIVE_REVIEW_MODEL
            if pack.get("control_level") == "routine"
            else "gpt-5.6-sol"
        ),
        "semantic_reasoning_effort": (
            "high"
            if pack.get("control_level") == "routine"
            else _semantic_review_effort(state)
        ),
        "next_action": {
            "id": "wait-review",
            "detail": (
                f"native lane {attempt.get('attempt_id')} is already running"
            ),
        },
    }


def _review_start_ready_response(
    *,
    owner: Path,
    state: dict[str, Any],
    change_id: str,
    operation_id: str,
    owner_selection: str,
    pack: dict[str, Any],
    relative_pack_path: str | None,
    pack_created: bool,
    native_required: bool,
    native_reused: bool,
    native_entry: dict[str, Any] | None,
    changed: bool,
    prepared_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    completed_semantic = next(
        (
            item
            for item in reversed(state["reviews"])
            if isinstance(item, dict)
            and item.get("review_id") == pack["review_id"]
            and item.get("kind") == "semantic"
            and item.get("status") == "completed"
            and isinstance(item.get("context_manifest_path"), str)
        ),
        None,
    )
    if completed_semantic is not None:
        context_path = safe_resolve(
            owner,
            completed_semantic["context_manifest_path"],
            must_exist=True,
        )
        if sha256_file(context_path) != completed_semantic.get("context_digest"):
            raise IntegrityError("Completed semantic context digest mismatch")
        context = {
            "manifest_path": completed_semantic["context_manifest_path"],
            "manifest": read_json(context_path),
        }
    elif prepared_context is not None:
        context = prepared_context
    else:
        context = (
            _review_context_v2(owner, change_id=change_id, pack=pack)
            if pack.get("context_contract") == REVIEW_CONTEXT_CONTRACT
            else build_context(
                owner,
                change_id=change_id,
                phase="review",
                include=[],
                exclude=[],
                dry_run=False,
            )
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
        "status": "completed",
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "operation_id": operation_id,
        "owner_root": str(owner),
        "owner_selection": owner_selection,
        "review_id": pack["review_id"],
        "review_pack_path": relative_pack_path,
        "pack_created": pack_created,
        "review_mode": pack.get("review_mode", "full"),
        "control_level": pack.get("control_level"),
        "risk_lenses": pack.get("risk_lenses", []),
        "required_prior_findings": pack.get("required_prior_findings", []),
        "native_required": native_required,
        "native_reused": native_required and native_reused,
        "native_argv": native_entry.get("argv") if native_entry else None,
        "native": native_entry,
        "native_coverage": native_coverage,
        "review_context_path": context["manifest_path"],
        "review_context_digest": context["manifest"]["manifest_digest"],
        "semantic_model": (
            NATIVE_REVIEW_MODEL
            if pack.get("control_level") == "routine"
            else "gpt-5.6-sol"
        ),
        "semantic_reasoning_effort": (
            "high"
            if pack.get("control_level") == "routine"
            else _semantic_review_effort(state)
        ),
        "next_action": {
            "id": "run-semantic-review",
            "detail": context["manifest_path"],
        },
    }


def review_start(
    root: Path,
    *,
    change_id: str,
    pack_path: str | None,
    operation_id: str | None,
    dry_run: bool = False,
    stream_callback: Any | None = None,
) -> dict[str, Any]:
    effective_operation_id = operation_id or str(uuid.uuid4())
    owner, state, pack, relative_pack_path, owner_selection = _resolve_review_pack(
        root,
        change_id=change_id,
        pack_path=pack_path,
        allow_missing_current=pack_path is None,
    )
    pack_created = False
    if pack is None:
        latest_result = _latest_review_result(state)
        if latest_result is None:
            return {
                "ok": False,
                "dry_run": dry_run,
                "changed": False,
                "change_id": change_id,
                "state_revision": state["state_revision"],
                "operation_id": effective_operation_id,
                "owner_root": str(owner),
                "owner_selection": owner_selection,
                "review_id": None,
                "review_pack_path": None,
                "pack_created": False,
                "next_action": {
                    "id": "provide-review-base",
                    "detail": (
                        "first review has no prepared ReviewPack; "
                        "run review-ready with --base BASE"
                    ),
                },
            }
        _, prior_report = _read_review_result(owner, latest_result)
        inferred_base = (
            prior_report.get("epic_base_sha")
            or prior_report.get("base_sha")
        )
        if not isinstance(inferred_base, str) or not inferred_base:
            raise IntegrityError("Latest ReviewIR cannot provide an epic base SHA")
        prepared = review_ready(
            owner,
            change_id=change_id,
            base_ref=inferred_base,
            expected_revision=state["state_revision"],
            operation_id=(
                f"{effective_operation_id}:prepare:{git_head(owner)}"
            ),
            dry_run=dry_run,
        )
        if not prepared["ok"]:
            return {
                "ok": False,
                "dry_run": dry_run,
                "changed": False,
                "change_id": change_id,
                "state_revision": prepared["state_revision"],
                "operation_id": effective_operation_id,
                "owner_root": str(owner),
                "owner_selection": owner_selection,
                "review_id": None,
                "review_pack_path": None,
                "pack_created": False,
                "next_action": prepared["next_action"],
            }
        pack = prepared["review_pack"]
        relative_pack_path = prepared["review_pack_path"]
        pack_created = True
        if not dry_run:
            state = StateStore(owner).load(change_id)
    if pack is None:
        raise IntegrityError("ReviewPack preparation did not return a pack")
    lane_operation_root = f"{effective_operation_id}:{pack['review_id']}"
    _validate_review_pack_current(owner, state=state, pack=pack)
    native_required = "native-diff" in pack["required_lanes"]
    budget = review_budget(owner, pack["control_level"])
    native_entry = _successful_native_entry(
        owner,
        state=state,
        review_id=pack["review_id"],
        pack=pack,
    )
    native_was_reused = native_entry is not None
    predicted_attempt_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"dls:{change_id}:{pack['review_id']}:native:1:{lane_operation_root}",
        )
    )
    predicted_output_path = (
        f".dls/cache/reviews/{change_id}/{pack['review_id']}/"
        f"native-final-{predicted_attempt_id}.txt"
    )
    argv = _native_review_argv(
        pack,
        "<dls-native-output>",
        working_directory="<dls-native-workspace>",
    )
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
            "pack_created": pack_created,
            "review_mode": pack.get("review_mode", "full"),
            "control_level": pack.get("control_level"),
            "risk_lenses": pack.get("risk_lenses", []),
            "required_prior_findings": pack.get("required_prior_findings", []),
            "native_required": native_required,
            "native_reused": native_entry is not None,
            "native_argv": argv if native_required else None,
            "native": native_entry,
            "review_context_path": None,
            "review_context_digest": context["manifest"]["manifest_digest"],
            "semantic_model": (
                NATIVE_REVIEW_MODEL
                if pack.get("control_level") == "routine"
                else "gpt-5.6-sol"
            ),
            "semantic_reasoning_effort": (
                "high"
                if pack.get("control_level") == "routine"
                else _semantic_review_effort(state)
            ),
            "next_action": {
                "id": "start-review",
                "detail": "dry-run review preflight is ready",
            },
        }
    prepared_context: dict[str, Any] | None = None
    if pack.get("control_level") == "routine":
        context_entry = native_entry
        if context_entry is None:
            context_entry = next(
                (
                    item
                    for item in reversed(
                        _review_lane_entries(
                            state,
                            review_id=pack["review_id"],
                            lane_key="native",
                        )
                    )
                    if isinstance(item.get("context_manifest_path"), str)
                ),
                None,
            )
        existing_context_path = (
            context_entry.get("context_manifest_path") if context_entry else None
        )
        if isinstance(existing_context_path, str) and "context/" in existing_context_path:
            resolved_context = safe_resolve(
                owner, existing_context_path, must_exist=True
            )
            if sha256_file(resolved_context) != context_entry.get("context_digest"):
                raise IntegrityError("Routine review context digest mismatch")
            prepared_context = {
                "manifest_path": existing_context_path,
                "manifest": read_json(resolved_context),
            }
        else:
            prepared_context = _review_context_v2(
                owner, change_id=change_id, pack=pack
            )
    changed = pack_created
    if native_required and native_entry is None:
        state_store = StateStore(owner)
        while native_entry is None:
            state = state_store.load(change_id)
            attempts = _review_lane_entries(
                state,
                review_id=pack["review_id"],
                lane_key="native",
            )
            running = next(
                (
                    item
                    for item in reversed(attempts)
                    if item.get("status") == "running"
                ),
                None,
            )
            if running is not None:
                if _process_is_alive(running.get("runner_pid")) and not _attempt_lease_expired(
                    running
                ):
                    return _lane_wait_response(
                        change_id=change_id,
                        state=state,
                        operation_id=effective_operation_id,
                        owner=owner,
                        owner_selection=owner_selection,
                        pack=pack,
                        relative_pack_path=relative_pack_path,
                        pack_created=pack_created,
                        attempt=running,
                    )
                state, _, abandoned = state_store.finish_review_lane(
                    change_id,
                    attempt_id=running["attempt_id"],
                    expected_status="running",
                    updates={
                        "status": "abandoned",
                        "completed_at": utc_now(),
                        "failure_reason": "runner process disappeared or lease expired",
                    },
                )
                changed = changed or abandoned
                continue
            completed = next(
                (
                    item
                    for item in reversed(attempts)
                    if item.get("status") == "completed"
                ),
                None,
            )
            if completed is not None:
                native_entry = _successful_native_entry(
                    owner,
                    state=state,
                    review_id=pack["review_id"],
                    pack=pack,
                )
                if native_entry is None:
                    state, _, invalidated = state_store.finish_review_lane(
                        change_id,
                        attempt_id=completed["attempt_id"],
                        expected_status="completed",
                        updates={
                            "status": "incompatible-workspace",
                            "completed_at": utc_now(),
                            "failure_reason": (
                                "native attempt lacks the exact-HEAD standalone "
                                "workspace provenance required by this ReviewPack"
                            ),
                        },
                    )
                    changed = changed or invalidated
                    continue
                native_was_reused = True
                return _review_start_ready_response(
                    owner=owner,
                    state=state,
                    change_id=change_id,
                    operation_id=effective_operation_id,
                    owner_selection=owner_selection,
                    pack=pack,
                    relative_pack_path=relative_pack_path,
                    pack_created=pack_created,
                    native_required=native_required,
                    native_reused=native_was_reused,
                    native_entry=native_entry,
                    changed=changed,
                    prepared_context=prepared_context,
                )
            terminal = attempts[-1] if attempts else None
            if terminal is not None and terminal.get("status") == "budget-exceeded":
                state, recovered_budget = _recover_legacy_double_counted_budget_attempt(
                    owner,
                    state_store=state_store,
                    change_id=change_id,
                    attempt=terminal,
                )
                if recovered_budget:
                    changed = True
                    continue
                return {
                    "ok": True,
                    "dry_run": False,
                    "changed": changed,
                    "status": "failed",
                    "change_id": change_id,
                    "state_revision": state["state_revision"],
                    "operation_id": effective_operation_id,
                    "owner_root": str(owner),
                    "owner_selection": owner_selection,
                    "review_id": pack["review_id"],
                    "review_pack_path": relative_pack_path,
                    "pack_created": pack_created,
                    "review_result_path": None,
                    "native": terminal,
                    "next_action": {
                        "id": "inspect-review-budget",
                        "detail": terminal.get("failure_reason")
                        or "native lane exceeded its budget",
                    },
                }
            if (
                terminal is not None
                and terminal.get("status") == "invalid-output"
            ):
                state, native_entry, recovered = _recover_native_plaintext_projection(
                    owner,
                    state_store=state_store,
                    change_id=change_id,
                    pack=pack,
                    entry=terminal,
                )
                changed = changed or recovered
                native_was_reused = True
                return _review_start_ready_response(
                    owner=owner,
                    state=state,
                    change_id=change_id,
                    operation_id=effective_operation_id,
                    owner_selection=owner_selection,
                    pack=pack,
                    relative_pack_path=relative_pack_path,
                    pack_created=pack_created,
                    native_required=native_required,
                    native_reused=native_was_reused,
                    native_entry=native_entry,
                    changed=changed,
                    prepared_context=prepared_context,
                )
            if terminal is not None and terminal.get("status") not in (
                RETRYABLE_REVIEW_LANE_STATUSES
            ):
                raise IntegrityError(
                    "Native review already finished without success: "
                    f"status={terminal.get('status')}"
                )
            if len(attempts) >= REVIEW_LANE_MAX_ATTEMPTS:
                raise IntegrityError(
                    "Native review exhausted automatic attempts: "
                    f"status={terminal.get('status') if terminal else 'unknown'}"
                )
            ordinal = len(attempts) + 1
            lane_operation_id = (
                lane_operation_root
                if ordinal == 1
                else f"{lane_operation_root}:retry-{ordinal}"
            )
            attempt_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    (
                        f"dls:{change_id}:{pack['review_id']}:native:"
                        f"{ordinal}:{lane_operation_id}"
                    ),
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
            argv = _native_review_argv(
                pack,
                "<dls-native-output>",
                working_directory="<dls-native-workspace>",
            )
            routine_review = pack.get("control_level") == "routine"
            native_schema_path = (
                SCHEMAS_ROOT / "review-decision.schema.json"
                if routine_review
                else SCHEMAS_ROOT / "native-review.schema.json"
            )
            attempt_context_path = relative_pack_path
            if routine_review:
                if prepared_context is None:
                    raise IntegrityError("Routine review context was not prepared")
                attempt_context_path = prepared_context["manifest_path"]
            snapshot_before = git_source_snapshot_digest(owner)
            proposed_attempt = {
                "review_id": pack["review_id"],
                "kind": "native",
                "lane_key": "native",
                "attempt_id": attempt_id,
                "attempt_ordinal": ordinal,
                "operation_id": lane_operation_id,
                "runner_pid": os.getpid(),
                "runner_contract": pack.get("runner_contract", REVIEW_RUNNER_CONTRACT),
                "dls_version": VERSION,
                "native_workspace_contract": pack.get(
                    "native_workspace_contract", NATIVE_WORKSPACE_CONTRACT
                ),
                "workspace_isolation": NATIVE_WORKSPACE_ISOLATION,
                "base_sha": pack.get("comparison_base_sha", pack["base_sha"]),
                "head_sha": pack["head_sha"],
                "pack_digest": pack["pack_digest"],
                "model": NATIVE_REVIEW_MODEL,
                "reasoning_effort": NATIVE_REVIEW_REASONING_EFFORT,
                "argv": argv,
                "prompt_path": (
                    "assets/review-prompts/routine.md"
                    if routine_review
                    else "builtin:codex-exec-review"
                ),
                "prompt_digest": sha256_bytes(
                    (
                        _routine_review_prompt(pack)
                        if routine_review
                        else (
                            "codex-exec-review/builtin-v1\n"
                            f"base={pack.get('comparison_base_sha', pack['base_sha'])}\n"
                        )
                    ).encode("utf-8")
                ),
                "schema_path": (
                    (
                        "assets/schemas/review-decision.schema.json"
                        if routine_review
                        else "assets/schemas/native-review.schema.json"
                    )
                    if pack.get("native_output_contract") == NATIVE_OUTPUT_CONTRACT
                    else "builtin:codex-review-bounded-text"
                ),
                "schema_digest": (
                    sha256_file(native_schema_path)
                    if pack.get("native_output_contract") == NATIVE_OUTPUT_CONTRACT
                    else sha256_bytes(b"codex-exec-review/bounded-text-v1\n")
                ),
                "context_manifest_path": attempt_context_path,
                "context_digest": sha256_file(
                    safe_resolve(owner, attempt_context_path, must_exist=True)
                ),
                "output_path": relative_output_path,
                "transcript_path": relative_transcript_path,
                "source_snapshot_before": snapshot_before,
                "started_at": utc_now(),
            }
            state, claimed_attempt, claimed = state_store.claim_review_lane(
                change_id,
                attempt=proposed_attempt,
                operation_kind="review-start",
                max_attempts=REVIEW_LANE_MAX_ATTEMPTS,
            )
            changed = changed or claimed
            if not claimed:
                if claimed_attempt.get("status") == "running":
                    return _lane_wait_response(
                        change_id=change_id,
                        state=state,
                        operation_id=effective_operation_id,
                        owner=owner,
                        owner_selection=owner_selection,
                        pack=pack,
                        relative_pack_path=relative_pack_path,
                        pack_created=pack_created,
                        attempt=claimed_attempt,
                    )
                continue
            if stream_callback is not None:
                stream_callback(
                    {
                        "event": "lane-transition",
                        "change_id": change_id,
                        "review_id": pack["review_id"],
                        "lane": "native",
                    }
                )
            break

        output_path = safe_resolve(owner, relative_output_path)
        transcript_path = safe_resolve(owner, relative_transcript_path)
        normalized_output_relative = (
            f".dls/cache/reviews/{change_id}/{pack['review_id']}/"
            f"native-normalized-{attempt_id}.json"
        )
        normalized_output_path = safe_resolve(owner, normalized_output_relative)
        if output_path.exists() or transcript_path.exists():
            raise IntegrityError(
                "Native review cache already exists without matching state; "
                "retry with a new operation ID"
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        native_parent = Path(tempfile.mkdtemp(prefix="dls-native-review-"))
        native_workspace = native_parent / "checkout"
        runtime_output_path = native_parent / "last-message.json"
        workspace_snapshot_before = snapshot_before
        workspace_snapshot_after = snapshot_before
        workspace_head_sha: str | None = None
        runtime_output_bytes = 0
        native_failure_reason: str | None = None
        try:
            native_workspace = _create_native_review_workspace(
                owner,
                head_sha=pack["head_sha"],
                parent=native_parent,
            )
            workspace_head_sha = git_head(native_workspace)
            workspace_snapshot_before = git_source_snapshot_digest(native_workspace)
            runtime_argv = _native_review_argv(
                pack,
                str(runtime_output_path),
                working_directory=str(native_workspace),
            )
            execution = _run_bounded_command(
                runtime_argv,
                cwd=native_workspace,
                environment=allowed_environment(["HOME", "CODEX_HOME"]),
                timeout_seconds=budget.timeout_seconds,
                max_output_bytes=budget.transcript_bytes,
                terminate_on_overflow=True,
                max_command_events=budget.command_events,
                heartbeat_callback=(
                    (
                        lambda elapsed, output_bytes, command_events: stream_callback(
                            {
                                "event": "heartbeat",
                                "change_id": change_id,
                                "review_id": pack["review_id"],
                                "lane": "native",
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
            workspace_snapshot_after = git_source_snapshot_digest(native_workspace)
        except Exception as exc:
            native_failure_reason = str(exc)
            failure_bytes = (
                f"{type(exc).__name__}: {exc}\n"
            ).encode("utf-8", errors="replace")
            execution = {
                "exit_code": 127,
                "timed_out": False,
                "overflow": False,
                "output": failure_bytes,
                "output_bytes": len(failure_bytes),
                "duration_seconds": 0.0,
            }
        finally:
            if runtime_output_path.is_file():
                runtime_output_bytes = runtime_output_path.stat().st_size
                with runtime_output_path.open("rb") as handle:
                    retained_final = handle.read(NATIVE_REVIEW_MAX_OUTPUT_BYTES)
                atomic_write_text(
                    output_path,
                    retained_final.decode("utf-8", errors="replace"),
                    backup=False,
                )
            shutil.rmtree(native_parent, ignore_errors=True)
        transcript_text = execution["output"].decode("utf-8", errors="replace")
        atomic_write_text(transcript_path, transcript_text, backup=False)
        output_exists = output_path.is_file()
        output_bytes = runtime_output_bytes if output_exists else 0
        output_overflow = output_bytes > NATIVE_REVIEW_MAX_OUTPUT_BYTES
        output_digest = sha256_file(output_path) if output_exists else None
        snapshot_after = git_source_snapshot_digest(owner)
        status_value = "completed"
        failure_reason: str | None = native_failure_reason
        native_output_format: str | None = None
        native_plaintext_projection_contract: str | None = None
        native_decision_status: str | None = None
        native_transcript_validation_contract: str | None = None
        native_recovery_status: str | None = None
        native_recovery_error: str | None = None
        if native_failure_reason is not None:
            status_value = "failed"
        elif execution["timed_out"]:
            status_value = "budget-exceeded"
            failure_reason = (
                f"native lane exceeded duration budget={budget.timeout_seconds}s"
            )
        elif execution["exit_code"] != 0:
            status_value = (
                "budget-exceeded"
                if execution.get("budget_exceeded") or execution.get("overflow")
                else "failed"
            )
        elif not output_exists or output_bytes == 0:
            status_value = "missing-output"
        elif output_overflow:
            status_value = "output-cap"
        elif pack.get("native_output_contract") == NATIVE_OUTPUT_CONTRACT:
            try:
                native_payload = read_json(output_path)
            except (OSError, json.JSONDecodeError, IntegrityError) as exc:
                try:
                    raw_text = output_path.read_text(encoding="utf-8")
                    try:
                        native_payload = (
                            _routine_plaintext_decision(raw_text, pack=pack)
                            if routine_review
                            else _native_plaintext_projection(raw_text)
                        )
                    except IntegrityError as strict_error:
                        if routine_review:
                            raise
                        native_payload = _native_indeterminate_plaintext_projection(
                            raw_text,
                            transcript_text=transcript_text,
                        )
                        native_output_format = (
                            "codex-review-plaintext-indeterminate"
                        )
                        native_plaintext_projection_contract = (
                            NATIVE_INDETERMINATE_PROJECTION_CONTRACT
                        )
                        native_decision_status = "indeterminate"
                        native_transcript_validation_contract = (
                            NATIVE_TRANSCRIPT_VALIDATION_CONTRACT
                        )
                        native_recovery_status = "recovered"
                    atomic_write_json(
                        normalized_output_path,
                        native_payload,
                        backup=False,
                    )
                    if native_output_format is None:
                        native_output_format = "codex-review-plaintext"
                        native_plaintext_projection_contract = (
                            NATIVE_PLAINTEXT_PROJECTION_CONTRACT
                        )
                        native_decision_status = "determinate"
                        native_recovery_status = "recovered"
                except (OSError, UnicodeError, IntegrityError) as parse_exc:
                    status_value = "invalid-output"
                    native_recovery_status = "unsafe"
                    native_recovery_error = redact_text(str(parse_exc))
                    failure_reason = (
                        "native structured output is invalid and plaintext "
                        "fallback is unsafe: "
                        f"{redact_text(str(parse_exc))}; JSON error: "
                        f"{redact_text(str(exc))}"
                    )
            else:
                if routine_review and (
                    not isinstance(native_payload, dict)
                    or native_payload.get("verdict")
                    not in {"review-clear", "not-clear", "blocked"}
                    or not isinstance(native_payload.get("summary"), str)
                    or not isinstance(native_payload.get("findings"), list)
                    or not isinstance(
                        native_payload.get("prior_finding_verdicts"), list
                    )
                ):
                    status_value = "invalid-output"
                    failure_reason = (
                        "routine native output requires a complete review decision"
                    )
                elif not routine_review and (
                    not isinstance(native_payload, dict)
                    or not isinstance(native_payload.get("summary"), str)
                    or not isinstance(native_payload.get("findings"), list)
                ):
                    status_value = "invalid-output"
                    failure_reason = "native structured output requires summary and findings"
                elif routine_review:
                    atomic_write_json(
                        normalized_output_path,
                        native_payload,
                        backup=False,
                    )
                    native_output_format = "structured-json"
                    native_decision_status = "determinate"
                else:
                    native_output_format = "structured-json"
                    native_decision_status = "determinate"
        if status_value == "completed" and (
            snapshot_after != snapshot_before
            or workspace_snapshot_after != workspace_snapshot_before
        ):
            status_value = "source-changed"
        usage = _codex_usage_from_output(execution["output"])
        budget_failure = token_budget_failure(
            usage,
            aggregate_before=0,
            budget=budget,
        )
        if status_value == "completed" and budget_failure is not None:
            status_value = "budget-exceeded"
            failure_reason = f"native {budget_failure}"
        final_updates = {
            "status": status_value,
            "output_path": relative_output_path if output_exists else None,
            "output_digest": output_digest,
            "output_bytes": output_bytes,
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
            "native_output_format": native_output_format,
            "native_plaintext_projection_contract": (
                native_plaintext_projection_contract
            ),
            "native_decision_status": native_decision_status,
            "native_transcript_validation_contract": (
                native_transcript_validation_contract
            ),
            "native_recovery_status": native_recovery_status,
            "native_recovery_error": native_recovery_error,
            "exit_code": execution["exit_code"],
            "timed_out": execution["timed_out"],
            "overflow": output_overflow,
            "transcript_path": relative_transcript_path,
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
            "budget": {
                "aggregate_tokens": budget.aggregate_tokens,
                "lane_tokens": budget.lane_tokens,
                "command_events": budget.command_events,
                "timeout_seconds": budget.timeout_seconds,
                "transcript_bytes": budget.transcript_bytes,
            },
            "duration_seconds": execution["duration_seconds"],
            "source_snapshot_digest": snapshot_after,
            "workspace_head_sha": workspace_head_sha,
            "workspace_source_snapshot_before": workspace_snapshot_before,
            "workspace_source_snapshot_after": workspace_snapshot_after,
            "failure_reason": failure_reason,
            "completed_at": utc_now(),
        }
        try:
            updated, native_entry, finalized = state_store.finish_review_lane(
                change_id,
                attempt_id=attempt_id,
                expected_status="running",
                updates=final_updates,
            )
            changed = changed or finalized
        except Exception:
            output_path.unlink(missing_ok=True)
            transcript_path.unlink(missing_ok=True)
            normalized_output_path.unlink(missing_ok=True)
            raise
        state = updated
        if status_value in RETRYABLE_REVIEW_LANE_STATUSES:
            retried = review_start(
                owner,
                change_id=change_id,
                pack_path=str(safe_resolve(owner, relative_pack_path, must_exist=True)),
                operation_id=effective_operation_id,
                dry_run=False,
                stream_callback=stream_callback,
            )
            retried["pack_created"] = pack_created or retried.get(
                "pack_created",
                False,
            )
            retried["changed"] = changed or retried.get("changed", False)
            return retried
        if status_value != "completed":
            if status_value == "budget-exceeded":
                return {
                    "ok": True,
                    "dry_run": False,
                    "changed": changed,
                    "status": "failed",
                    "change_id": change_id,
                    "state_revision": state["state_revision"],
                    "operation_id": effective_operation_id,
                    "owner_root": str(owner),
                    "owner_selection": owner_selection,
                    "review_id": pack["review_id"],
                    "review_pack_path": relative_pack_path,
                    "pack_created": pack_created,
                    "review_result_path": None,
                    "native": native_entry,
                    "next_action": {
                        "id": "inspect-review-budget",
                        "detail": failure_reason or "native lane exceeded its budget",
                    },
                }
            raise IntegrityError(
                f"Native review did not complete: status={status_value}; "
                f"transcript={relative_transcript_path}"
            )
        _validate_review_pack_current(owner, state=state, pack=pack)
    return _review_start_ready_response(
        owner=owner,
        state=state,
        change_id=change_id,
        operation_id=effective_operation_id,
        owner_selection=owner_selection,
        pack=pack,
        relative_pack_path=relative_pack_path,
        pack_created=pack_created,
        native_required=native_required,
        native_reused=native_was_reused,
        native_entry=native_entry,
        changed=changed,
        prepared_context=prepared_context,
    )


def _state_lane_attempt(
    state: dict[str, Any],
    *,
    review_id: str,
    attempt_id: object,
) -> dict[str, Any]:
    if not isinstance(attempt_id, str) or not attempt_id:
        raise IntegrityError("ReviewIR lane is missing attempt_id")
    attempt = next(
        (
            item
            for item in state["reviews"]
            if isinstance(item, dict)
            and item.get("review_id") == review_id
            and item.get("attempt_id") == attempt_id
        ),
        None,
    )
    if not attempt or attempt.get("status") != "completed":
        raise IntegrityError(
            f"ReviewIR lane has no completed state attempt: {attempt_id}"
        )
    if attempt.get("runner_contract") not in {
        None,
        LEGACY_REVIEW_RUNNER_CONTRACT,
        REVIEW_RUNNER_CONTRACT,
    }:
        raise IntegrityError("Review lane runner contract mismatch")
    return attempt


def _assert_lane_matches_state(
    lane: dict[str, Any],
    attempt: dict[str, Any],
    *,
    fields: tuple[str, ...],
) -> None:
    for field in fields:
        if lane.get(field) != attempt.get(field):
            raise IntegrityError(
                f"ReviewIR state-owned provenance mismatch: {field}"
            )


def _validate_state_owned_review_provenance(
    root: Path,
    *,
    state: dict[str, Any],
    pack: dict[str, Any],
    report: dict[str, Any],
) -> None:
    review_id = pack["review_id"]
    lanes = report["lanes"]
    semantic = lanes["semantic"]
    semantic_attempt = _state_lane_attempt(
        state,
        review_id=review_id,
        attempt_id=semantic.get("attempt_id"),
    )
    _assert_lane_matches_state(
        semantic,
        semantic_attempt,
        fields=(
            "operation_id",
            "model",
            "reasoning_effort",
            "transcript_path",
            "transcript_digest",
        ),
    )
    context_path = safe_resolve(
        root,
        semantic["context_manifest_path"],
        must_exist=True,
    )
    if (
        semantic_attempt.get("context_manifest_path")
        != semantic.get("context_manifest_path")
        or semantic_attempt.get("context_digest") != sha256_file(context_path)
    ):
        raise IntegrityError("Semantic context provenance is not state-owned")
    if semantic_attempt.get("output_path") != semantic.get(
        "independent_draft_path"
    ) or semantic_attempt.get("output_digest") != semantic.get(
        "independent_draft_digest"
    ):
        raise IntegrityError("Semantic independent draft is not state-owned")
    for repair in semantic.get("repairs", []):
        if not isinstance(repair, dict):
            raise IntegrityError("ReviewIR decision repair provenance is invalid")
        repair_attempt = _state_lane_attempt(
            state,
            review_id=review_id,
            attempt_id=repair.get("repair_attempt_id"),
        )
        original_attempt = next(
            (
                item
                for item in state["reviews"]
                if isinstance(item, dict)
                and item.get("review_id") == review_id
                and item.get("attempt_id") == repair.get("original_attempt_id")
            ),
            None,
        )
        if not original_attempt or original_attempt.get("status") not in {
            "invalid-output",
            "completed",
        }:
            raise IntegrityError(
                "ReviewIR repair has no immutable original decision attempt"
            )
        expected = {
            "contract": repair_attempt.get("repair_contract"),
            "source_lane_key": repair_attempt.get("repair_source_lane_key"),
            "original_attempt_id": repair_attempt.get(
                "repair_original_attempt_id"
            ),
            "original_output_digest": repair_attempt.get(
                "repair_original_output_digest"
            ),
            "error_code": repair_attempt.get("repair_error_code"),
            "error_digest": repair_attempt.get("repair_error_digest"),
            "input_bundle_digest": repair_attempt.get("input_bundle_digest"),
            "repair_attempt_id": repair_attempt.get("attempt_id"),
            "repair_output_digest": repair_attempt.get("output_digest"),
            "model": repair_attempt.get("model"),
            "reasoning_effort": repair_attempt.get("reasoning_effort"),
            "started_at": repair_attempt.get("started_at"),
            "completed_at": repair_attempt.get("completed_at"),
            "transcript_digest": repair_attempt.get("transcript_digest"),
        }
        if any(repair.get(key) != value for key, value in expected.items()):
            raise IntegrityError("ReviewIR decision repair provenance is not state-owned")
        original_path = original_attempt.get("output_path")
        if (
            original_attempt.get("output_digest")
            != repair.get("original_output_digest")
            or not isinstance(original_path, str)
            or sha256_file(safe_resolve(root, original_path, must_exist=True))
            != repair.get("original_output_digest")
        ):
            raise IntegrityError("ReviewIR repair original output digest mismatch")
    for semantic_pass in semantic.get("passes", []):
        pass_attempt = _state_lane_attempt(
            state,
            review_id=review_id,
            attempt_id=semantic_pass.get("attempt_id"),
        )
        if (
            semantic_pass.get("operation_id") != pass_attempt.get("operation_id")
            or semantic_pass.get("draft_path") != pass_attempt.get("output_path")
            or semantic_pass.get("draft_digest") != pass_attempt.get("output_digest")
            or semantic_pass.get("transcript_path")
            != pass_attempt.get("transcript_path")
            or semantic_pass.get("transcript_digest")
            != pass_attempt.get("transcript_digest")
        ):
            raise IntegrityError("Semantic pass provenance is not state-owned")
    reconciliation = lanes.get("reconciliation")
    if not isinstance(reconciliation, dict) and pack.get("runner_contract") == LEGACY_REVIEW_RUNNER_CONTRACT:
        raise IntegrityError("ReviewIR is missing reconciliation provenance")
    if isinstance(reconciliation, dict):
        reconciliation_attempt = _state_lane_attempt(
            state,
            review_id=review_id,
            attempt_id=reconciliation.get("attempt_id"),
        )
        _assert_lane_matches_state(
            reconciliation,
            reconciliation_attempt,
            fields=(
                "operation_id",
                "model",
                "reasoning_effort",
                "prompt_path",
                "prompt_digest",
                "schema_path",
                "schema_digest",
                "output_path",
                "output_digest",
                "transcript_path",
                "transcript_digest",
                "source_snapshot_digest",
            ),
        )
    expected_lenses = [
        item["id"]
        for item in pack.get("risk_lenses", [])
        if pack.get("runner_contract") == LEGACY_REVIEW_RUNNER_CONTRACT
        or pack.get("review_mode") == "full"
    ]
    specialists = lanes.get("specialists", [])
    if [item.get("lens_id") for item in specialists] != expected_lenses:
        raise IntegrityError("State-owned specialist lanes do not match ReviewPack")
    for specialist in specialists:
        attempt = _state_lane_attempt(
            state,
            review_id=review_id,
            attempt_id=specialist.get("attempt_id"),
        )
        if attempt.get("lane_key") != f"specialist:{specialist['lens_id']}":
            raise IntegrityError("Specialist state lane key mismatch")
        if (
            specialist.get("draft_path") != attempt.get("output_path")
            or specialist.get("draft_digest") != attempt.get("output_digest")
        ):
            raise IntegrityError("Specialist draft is not state-owned")
    for lane in (
        semantic,
        reconciliation,
        *specialists,
    ):
        if not isinstance(lane, dict):
            continue
        for path_key, digest_key in (
            ("transcript_path", "transcript_digest"),
            ("output_path", "output_digest"),
            ("draft_path", "draft_digest"),
        ):
            relative = lane.get(path_key)
            digest = lane.get(digest_key)
            if relative is None and digest is None:
                continue
            if not isinstance(relative, str) or not isinstance(digest, str):
                raise IntegrityError(
                    f"ReviewIR lane has incomplete {path_key} provenance"
                )
            if sha256_file(safe_resolve(root, relative, must_exist=True)) != digest:
                raise IntegrityError(
                    f"ReviewIR lane cache digest mismatch: {path_key}"
                )


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
    runner_contract = pack.get("runner_contract")
    if runner_contract is not None:
        if runner_contract not in {
            LEGACY_REVIEW_RUNNER_CONTRACT,
            REVIEW_RUNNER_CONTRACT,
        }:
            raise IntegrityError(
                f"Unsupported ReviewPack runner contract: {runner_contract}"
            )
        if report.get("runner_contract") != runner_contract:
            raise IntegrityError(
                "ReviewIR is missing the state-owned runner contract"
            )
    native_entry = _successful_native_entry(
        root,
        state=state,
        review_id=review_id,
        pack=pack,
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
        optional_native_fields = (
            "normalized_output_path",
            "normalized_output_digest",
            "native_output_format",
            "native_decision_status",
            "native_plaintext_projection_contract",
            "native_transcript_validation_contract",
        )
        require_projection_provenance = (
            native_entry.get("native_decision_status") == "indeterminate"
        )
        for field in optional_native_fields:
            report_value = native_lane.get(field)
            if (
                require_projection_provenance or report_value is not None
            ) and report_value != native_entry.get(field):
                raise IntegrityError(
                    f"ReviewIR native provenance mismatch: {field}"
                )
        if pack.get("native_workspace_contract") is not None:
            for field in (
                "native_workspace_contract",
                "workspace_isolation",
                "workspace_head_sha",
                "workspace_source_snapshot_before",
                "workspace_source_snapshot_after",
            ):
                if native_lane.get(field) != native_entry.get(field):
                    raise IntegrityError(
                        f"ReviewIR native workspace provenance mismatch: {field}"
                    )
        if runner_contract in {
            LEGACY_REVIEW_RUNNER_CONTRACT,
            REVIEW_RUNNER_CONTRACT,
        }:
            for field in (
                "operation_id",
                "prompt_path",
                "prompt_digest",
                "schema_path",
                "schema_digest",
                "context_manifest_path",
                "context_digest",
                "transcript_path",
                "transcript_digest",
            ):
                if native_lane.get(field) != native_entry.get(field):
                    raise IntegrityError(
                        f"ReviewIR native provenance mismatch: {field}"
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
    if runner_contract in {
        LEGACY_REVIEW_RUNNER_CONTRACT,
        REVIEW_RUNNER_CONTRACT,
    }:
        _validate_state_owned_review_provenance(
            root,
            state=state,
            pack=pack,
            report=report,
        )
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
    if context_manifest.get("contract") == REVIEW_CONTEXT_CONTRACT:
        projection_entry = next(
            (
                item
                for item in context_manifest.get("inputs", [])
                if isinstance(item, dict)
                and item.get("reason") == "active-review-pack"
            ),
            None,
        )
        if not projection_entry:
            raise IntegrityError("Semantic review context has no compact pack projection")
        projection_path = safe_resolve(
            root, projection_entry["path"], must_exist=True
        )
        if (
            sha256_file(projection_path) != projection_entry.get("sha256")
            or read_json(projection_path).get("pack_digest") != pack["pack_digest"]
        ):
            raise IntegrityError("Compact ReviewPack projection digest mismatch")
    elif not any(
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
            differing_fields = sorted(
                key
                for key in set(recorded) | set(report)
                if recorded.get(key) != report.get(key)
            )
            raise IntegrityError(
                f"Review result already imported with different content: {review_id}; "
                f"fields={','.join(differing_fields)}"
            )
        remediation_path = _existing_remediation_manifest_path(
            root,
            change_id=change_id,
            review_entry=existing_result,
            review_id=review_id,
        )
        needs_remediation = bool(_review_actionable_findings(report))
        if remediation_path is not None:
            remediation_path, _ = _load_remediation_manifest(
                root,
                change_id=change_id,
                prior_review={
                    "review_id": review_id,
                    "result_path": relative_path,
                    "result_digest": existing_result.get("result_digest")
                    or _review_result_digest(report),
                    "head_sha": report["head_sha"],
                    "definition_digest": report["definition_digest"],
                    "remediation_manifest_path": existing_result.get(
                        "remediation_manifest_path"
                    ),
                    "remediation_manifest_digest": existing_result.get(
                        "remediation_manifest_digest"
                    ),
                },
            )
        imported = {
            "ok": report["verdict"] == "review-clear",
            "dry_run": False,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "review_result_path": relative_path,
            "remediation_manifest_path": remediation_path,
            "verdict": report["verdict"],
            "finding_counts": _finding_counts(report["findings"]),
            "next_action": {
                "id": (
                    "remediate-findings"
                    if remediation_path is not None
                    else (
                        "recover-remediation-manifest"
                        if needs_remediation
                        else (
                            "accept-review"
                            if report["verdict"] == "review-clear"
                            else "resolve-review-blocker"
                        )
                    )
                ),
                "detail": remediation_path or relative_path,
            },
        }
        from .delivery_receipt import delivery_receipt

        imported["delivery_receipt"] = delivery_receipt(
            root, change_id=change_id
        )
        return imported
    if existing_operation:
        raise IntegrityError(f"Review import operation has no matching result: {effective_operation_id}")
    _require_revision(state, expected_revision)
    result_digest = _review_result_digest(report)
    result_record = {
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
    remediation_manifest = _build_remediation_manifest(
        root,
        change_id=change_id,
        review_entry=result_record,
        report=report,
        pack_entry=pack_entry,
        pack=pack,
        result_path=relative_path,
        pack_path=pack_path,
        origin="review-import",
    )
    remediation_path = (
        _canonical_remediation_manifest_path(change_id, review_id)
        if remediation_manifest is not None
        else None
    )
    if remediation_manifest is not None and remediation_path is not None:
        result_record["remediation_manifest_path"] = remediation_path
        result_record["remediation_manifest_digest"] = remediation_manifest[
            "manifest_digest"
        ]
    if dry_run:
        return {
            "ok": report["verdict"] == "review-clear",
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "operation_id": effective_operation_id,
            "review_result_path": None,
            "remediation_manifest_path": None,
            "projected_remediation_manifest_path": remediation_path,
            "verdict": report["verdict"],
            "finding_counts": _finding_counts(report["findings"]),
            "next_action": {
                "id": (
                    "remediate-findings"
                    if remediation_manifest is not None
                    else (
                        "accept-review"
                        if report["verdict"] == "review-clear"
                        else "resolve-review-blocker"
                    )
                ),
                "detail": remediation_path or relative_path,
            },
        }

    def mutate(value: dict[str, Any]) -> None:
        if any(
            entry.get("review_id") == review_id and entry.get("kind") == "result"
            for entry in value["reviews"]
        ):
            return
        value["reviews"].append(dict(result_record))
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

    artifacts = [(safe_resolve(root, relative_path), report)]
    if remediation_manifest is not None and remediation_path is not None:
        artifacts.append(
            (safe_resolve(root, remediation_path), remediation_manifest)
        )
    updated, changed = state_store.mutate_with_immutable_artifacts(
        change_id,
        expected_revision=expected_revision,
        operation_id=effective_operation_id,
        operation_kind=operation_kind,
        artifacts=artifacts,
        mutator=mutate,
    )
    imported = {
        "ok": report["verdict"] == "review-clear",
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "operation_id": effective_operation_id,
        "review_result_path": relative_path,
        "remediation_manifest_path": remediation_path,
        "verdict": report["verdict"],
        "finding_counts": _finding_counts(report["findings"]),
        "next_action": {
            "id": (
                "remediate-findings"
                if remediation_manifest is not None
                else (
                    "accept-review"
                    if report["verdict"] == "review-clear"
                    else "resolve-review-blocker"
                )
            ),
            "detail": remediation_path or relative_path,
        },
    }
    from .delivery_receipt import delivery_receipt

    imported["delivery_receipt"] = delivery_receipt(root, change_id=change_id)
    return imported


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
    evidence = list(dict.fromkeys(evidence))
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
            "evidence": recorded["evidence"],
            "evidence_count": len(recorded["evidence"]),
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
            "evidence": record["evidence"],
            "evidence_count": len(record["evidence"]),
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
        "evidence": recorded["evidence"],
        "evidence_count": len(recorded["evidence"]),
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
            resolve_profile(
                root,
                config={"default_profile": profile_path.stem},
            )
        except ConfigError:
            profile_ok = False
    checks.append(_check("profiles:runtime", profile_ok, str(PROFILES_ROOT)))
    active_skills: list[str] = []
    skill_metadata_ok = True
    for skill_path in sorted((PLUGIN_ROOT / "skills").glob("*/SKILL.md")):
        text = skill_path.read_text(encoding="utf-8")
        yaml_path = skill_path.parent / "agents" / "openai.yaml"
        yaml_text = yaml_path.read_text(encoding="utf-8") if yaml_path.is_file() else ""
        expected_implicit = (
            "allow_implicit_invocation: true"
            if skill_path.parent.name == "dls-workflow"
            else "allow_implicit_invocation: false"
        )
        if (
            not text.startswith("---\n")
            or "description:" not in text.split("---", 2)[1]
            or expected_implicit not in yaml_text
        ):
            skill_metadata_ok = False
        active_skills.append(skill_path.parent.name)
    checks.append(
        _check(
            "skills:activation-policy",
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
    platform_profile: dict[str, Any] | None = None
    if config_path.is_file():
        try:
            config = load_config(root)
            platform_profile = resolve_profile(root, config=config)
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
        "platform_profile": (
            {
                "contract": platform_profile["contract"],
                "name": platform_profile["name"],
                "digest": platform_profile["digest"],
                "source": platform_profile["source"],
                "inheritance_chain": platform_profile["inheritance_chain"],
                "domain_capabilities": platform_profile["domain_capabilities"],
            }
            if platform_profile is not None
            else None
        ),
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
    contract_digest = command_contract_digest(root, command_id)
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
                "command_contract_digest": contract_digest,
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
        output_digest = execution["output_sha256"]
        failure_excerpt = ""
        if (
            execution["exit_code"] != 0
            or execution["timed_out"]
            or execution["overflow"]
        ):
            failure_excerpt = output_text[-4096:]
        summary = (
            f"command={command_id}; exit={execution['exit_code']}; "
            f"timeout={execution['timed_out']}; output_bytes={execution['output_bytes']}; "
            f"output_overflow={execution['overflow']}; output_sha256={output_digest}"
        )
        if failure_excerpt:
            summary += "\nexcerpt=" + failure_excerpt
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
                "spawn_error": execution["spawn_error"],
                "output_sha256": output_digest,
                "command_contract_digest": contract_digest,
                "redacted_log_path": str(output_path.relative_to(root)),
            },
        )
        evidence["validation"] = {
            "timed_out": execution["timed_out"],
            "output_overflow": execution["overflow"],
            "spawn_error": execution["spawn_error"],
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
            "platform_profile": manifest.get("platform_profile"),
            "decisions": manifest.get("decisions"),
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
    identifier_contract = pack.get("identifier_contract")
    if identifier_contract is not None and report.get("identifier_contract") != identifier_contract:
        raise IntegrityError("ReviewIR identifier contract does not match ReviewPack")
    for field in (
        "context_contract",
        "economy_contract",
        "budget_contract",
        "native_output_contract",
        "native_workspace_contract",
    ):
        if pack.get(field) is not None and report.get(field) != pack.get(field):
            raise IntegrityError(f"ReviewIR {field} does not match ReviewPack")
    pack_repair_contract = pack.get("decision_repair_contract")
    report_repair_contract = report.get("decision_repair_contract")
    if pack_repair_contract is not None and pack_repair_contract != REVIEW_DECISION_REPAIR_CONTRACT:
        raise IntegrityError("Unsupported ReviewPack decision repair contract")
    if report_repair_contract not in {None, REVIEW_DECISION_REPAIR_CONTRACT}:
        raise IntegrityError("Unsupported ReviewIR decision repair contract")
    if pack_repair_contract is not None and report_repair_contract not in {
        None,
        pack_repair_contract,
    }:
        raise IntegrityError("ReviewIR decision repair contract mismatch")
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
    expected_semantic_model = (
        "gpt-5.6-terra" if pack.get("control_level") == "routine" else "gpt-5.6-sol"
    )
    if semantic_lane["model"] != expected_semantic_model:
        raise IntegrityError(
            f"ReviewIR semantic lane must use {expected_semantic_model}"
        )
    if semantic_lane["reasoning_effort"] not in {"high", "xhigh"}:
        raise IntegrityError("ReviewIR semantic reasoning effort must be high or xhigh")
    repairs = semantic_lane.get("repairs", [])
    if not isinstance(repairs, list):
        raise IntegrityError("ReviewIR semantic repairs must be an array")
    if repairs and report_repair_contract != REVIEW_DECISION_REPAIR_CONTRACT:
        raise IntegrityError("ReviewIR repairs require the DLS repair contract")
    required_repair_fields = {
        "contract",
        "original_attempt_id",
        "original_output_digest",
        "error_code",
        "error_digest",
        "input_bundle_digest",
        "repair_attempt_id",
        "repair_output_digest",
        "model",
        "reasoning_effort",
        "transcript_digest",
    }
    for repair in repairs:
        if not isinstance(repair, dict) or not required_repair_fields.issubset(repair):
            raise IntegrityError("ReviewIR decision repair provenance is incomplete")
        if (
            repair.get("contract") != REVIEW_DECISION_REPAIR_CONTRACT
            or repair.get("model") != "gpt-5.6-sol"
            or repair.get("reasoning_effort") not in {"high", "xhigh"}
        ):
            raise IntegrityError("ReviewIR decision repair provenance is invalid")
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
            routine_clear = (
                pack.get("control_level") == "routine"
                and pack.get("runner_contract") == REVIEW_RUNNER_CONTRACT
                and pass_kinds == ["targeted"]
            )
            if (
                report["verdict"] == "review-clear"
                and not routine_clear
                and pass_kinds != ["targeted", "final-full"]
            ):
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
        expected_lenses = [
            item["id"]
            for item in pack["risk_lenses"]
            if pack.get("runner_contract") == LEGACY_REVIEW_RUNNER_CONTRACT
            or pack.get("review_mode") == "full"
        ]
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
    identifier_normalizations = report.get("identifier_normalizations", [])
    if not isinstance(identifier_normalizations, list):
        raise IntegrityError("ReviewIR identifier_normalizations must be an array")
    for item in identifier_normalizations:
        if not isinstance(item, dict):
            raise IntegrityError("ReviewIR identifier normalization must be an object")
        finding_id = item.get("finding_id")
        source = item.get("source")
        canonical = item.get("canonical")
        if (
            finding_id not in seen
            or item.get("field") != "ticket_ids"
            or not isinstance(source, str)
            or not source
            or not isinstance(canonical, str)
            or canonical not in pack["tickets"]
            or source == canonical
            or item.get("rule") != "unique-ticket-alias"
        ):
            raise IntegrityError("ReviewIR identifier normalization is invalid")
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
    findings = _canonical_review_findings(root, state)
    if not findings:
        return {"blocker": 0, "should-fix": 0, "note": 0}
    latest_dispositions = _latest_dispositions(state)
    closed = {
        finding_id
        for finding_id, disposition in latest_dispositions.items()
        if disposition["status"] in {"verified", "waived"}
    }
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
    proof_head_sha: str | None = None,
) -> tuple[bool, str]:
    ok, detail, _ = _required_evidence_status(
        root,
        state,
        stage=stage,
        proof_head_sha=proof_head_sha,
    )
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


def _codex_usage_from_output(output: bytes) -> dict[str, int] | None:
    """Return the last structured Codex usage event from a JSONL transcript."""
    latest: dict[str, int] | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith(b"{"):
            continue
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        normalized: dict[str, int] = {}
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                normalized[key] = value
        if normalized and any(normalized.values()):
            latest = normalized
    return latest


def _command_event_identity(event: dict[str, Any]) -> tuple[bool, str | None]:
    """Return whether a JSONL event is command-like and its logical invocation ID."""
    item = event.get("item")
    item_type = item.get("type") if isinstance(item, dict) else None
    event_type = event.get("type")
    command_like = item_type in {"command_execution", "mcp_tool_call", "tool_call"} or event_type in {
        "command.started",
        "command.completed",
        "tool.started",
        "tool.completed",
    }
    if not command_like:
        return False, None
    for value in (
        item.get("id") if isinstance(item, dict) else None,
        event.get("id"),
    ):
        if isinstance(value, str) and value:
            return True, value
    return True, None


def _logical_command_event_count(output: bytes) -> int:
    """Count logical command invocations in a complete Codex JSONL transcript."""
    seen: set[str] = set()
    count = 0
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith(b"{"):
            continue
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        command_like, event_id = _command_event_identity(event)
        if not command_like:
            continue
        if event_id is not None:
            if event_id in seen:
                continue
            seen.add(event_id)
        count += 1
    return count


def _run_bounded_command(
    argv: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout_seconds: int,
    max_output_bytes: int,
    terminate_on_overflow: bool = True,
    max_command_events: int | None = None,
    heartbeat_callback: Any | None = None,
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
            "spawn_error": True,
            "output": message[:max_output_bytes],
            "output_bytes": len(message),
            "output_sha256": sha256_bytes(message),
            "duration_seconds": time.monotonic() - started,
            "command_events": 0,
            "command_event_contract": COMMAND_EVENT_CONTRACT,
            "budget_exceeded": False,
            "budget_failure_kind": None,
        }
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    retained = bytearray()
    total = 0
    output_hasher = hashlib.sha256()
    timed_out = False
    overflow = False
    budget_exceeded = False
    command_budget_exceeded = False
    command_events = 0
    logical_command_ids: set[str] = set()
    line_buffer = bytearray()
    last_heartbeat = started

    def inspect_line(raw_line: bytes) -> None:
        nonlocal command_events, budget_exceeded, command_budget_exceeded
        line = raw_line.strip()
        if not line.startswith(b"{"):
            return
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(event, dict):
            return
        command_like, event_id = _command_event_identity(event)
        if command_like:
            # Codex JSONL reports one logical tool/command twice: once when the
            # item starts and once when it completes. Budget the invocation,
            # not the transport events. Anonymous legacy events still count
            # individually so older/fake runners retain their semantics.
            if event_id is not None:
                logical_key = f"command:{event_id}"
                if logical_key in logical_command_ids:
                    return
                logical_command_ids.add(logical_key)
            command_events += 1
            if max_command_events is not None and command_events > max_command_events:
                budget_exceeded = True
                command_budget_exceeded = True
                stop_process()

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
            if (
                heartbeat_callback is not None
                and time.monotonic() - last_heartbeat >= 60.0
            ):
                heartbeat_callback(elapsed, total, command_events)
                last_heartbeat = time.monotonic()
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
                output_hasher.update(chunk)
                line_buffer.extend(chunk)
                while b"\n" in line_buffer:
                    raw_line, _, remainder = line_buffer.partition(b"\n")
                    line_buffer[:] = remainder
                    inspect_line(raw_line)
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
    elif command_budget_exceeded:
        exit_code = 126
    elif overflow and terminate_on_overflow:
        exit_code = 125
    return {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "overflow": overflow,
        "spawn_error": False,
        "output": bytes(retained),
        "output_bytes": total,
        "output_sha256": output_hasher.hexdigest(),
        "duration_seconds": time.monotonic() - started,
        "command_events": command_events,
        "command_event_contract": COMMAND_EVENT_CONTRACT,
        "budget_exceeded": budget_exceeded,
        "budget_failure_kind": (
            "command-events"
            if command_budget_exceeded
            else ("transcript-bytes" if overflow and terminate_on_overflow else None)
        ),
    }
