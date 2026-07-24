"""Versioned DLS state and approval integrity."""

from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from .errors import IntegrityError, UsageError
from .io import FileLock, atomic_write_json, read_json, utc_now
from .repo import git_head, package_digest

WORK_KINDS = {"feature", "bug", "chore", "spike", "hotfix"}
CONTROL_LEVELS = {"routine", "standard", "critical"}
IMPACT_TAGS = {
    "public-api",
    "data-migration",
    "security-privacy",
    "auth",
    "money",
    "data-loss",
    "concurrency",
    "availability",
    "compatibility",
    "performance-cost",
    "external-dependency",
    "user-interface",
    "release",
    "architecture",
    "irreversible",
}
CHANGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_change_id(change_id: str) -> str:
    if not CHANGE_ID_PATTERN.fullmatch(change_id):
        raise UsageError("Change ID must use 1-64 letters, digits, dots, underscores, or hyphens")
    return change_id


def initial_state(
    *,
    change_id: str,
    slug: str,
    work_kind: str,
    control_level: str,
    impact_tags: list[str],
    artifacts: dict[str, dict[str, str]],
    operation_id: str,
) -> dict[str, Any]:
    state = {
        "schema_version": 1,
        "state_revision": 1,
        "change_id": validate_change_id(change_id),
        "slug": slug,
        "work_kind": work_kind,
        "control_level": control_level,
        "impact_tags": sorted(set(impact_tags)),
        "phase": "definition",
        "lifecycle": "draft",
        "artifacts": artifacts,
        "approvals": [],
        "tickets": {},
        "reviews": [],
        "finding_dispositions": [],
        "evidence": [],
        "operations": [
            {
                "id": operation_id,
                "kind": "state-create",
                "recorded_at": utc_now(),
            }
        ],
    }
    validate_state(state)
    return state


def validate_state(state: dict[str, Any]) -> None:
    if state.get("schema_version") != 1:
        raise IntegrityError("Unsupported state schema")
    revision = state.get("state_revision")
    if not isinstance(revision, int) or revision < 1:
        raise IntegrityError("state_revision must be a positive integer")
    change_id = state.get("change_id")
    if not isinstance(change_id, str):
        raise IntegrityError("change_id must be a string")
    validate_change_id(change_id)
    if state.get("work_kind") not in WORK_KINDS:
        raise IntegrityError(f"Invalid work_kind: {state.get('work_kind')!r}")
    if state.get("control_level") not in CONTROL_LEVELS:
        raise IntegrityError(f"Invalid control_level: {state.get('control_level')!r}")
    tags = state.get("impact_tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise IntegrityError("impact_tags must be a string array")
    unknown_tags = sorted(set(tags) - IMPACT_TAGS)
    if unknown_tags:
        raise IntegrityError(f"Unknown impact tags: {', '.join(unknown_tags)}")
    requirement_prefixes = state.get("requirement_prefixes", [])
    if not isinstance(requirement_prefixes, list) or not all(
        isinstance(prefix, str) and re.fullmatch(r"[A-Z][A-Z0-9]{0,15}", prefix)
        for prefix in requirement_prefixes
    ):
        raise IntegrityError("requirement_prefixes must be a valid string array")
    if "adopted" in state and not isinstance(state["adopted"], bool):
        raise IntegrityError("adopted must be a boolean")
    for key, expected_type in (
        ("artifacts", dict),
        ("approvals", list),
        ("tickets", dict),
        ("reviews", list),
        ("finding_dispositions", list),
        ("evidence", list),
        ("operations", list),
    ):
        if not isinstance(state.get(key), expected_type):
            raise IntegrityError(f"state.{key} must be {expected_type.__name__}")


class StateStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.directory = root / ".dls" / "state"

    def path(self, change_id: str) -> Path:
        return self.directory / f"{validate_change_id(change_id)}.json"

    def load(self, change_id: str) -> dict[str, Any]:
        state = read_json(self.path(change_id))
        validate_state(state)
        return state

    def create(self, state: dict[str, Any]) -> None:
        validate_state(state)
        path = self.path(state["change_id"])
        lock_path = path.with_suffix(path.suffix + ".lock")
        with FileLock(lock_path):
            if path.exists():
                raise IntegrityError(f"State already exists: {path}")
            atomic_write_json(path, state, backup=False)

    def mutate(
        self,
        change_id: str,
        *,
        expected_revision: int,
        operation_id: str | None,
        operation_kind: str,
        mutator: Callable[[dict[str, Any]], None],
    ) -> tuple[dict[str, Any], bool]:
        path = self.path(change_id)
        lock_path = path.with_suffix(path.suffix + ".lock")
        effective_operation_id = operation_id or str(uuid.uuid4())
        with FileLock(lock_path):
            state = self.load(change_id)
            existing_operation = next(
                (
                    operation
                    for operation in state["operations"]
                    if isinstance(operation, dict)
                    and operation.get("id") == effective_operation_id
                ),
                None,
            )
            if existing_operation:
                if existing_operation.get("kind") != operation_kind:
                    raise IntegrityError(
                        f"Operation ID already belongs to {existing_operation.get('kind')}: "
                        f"{effective_operation_id}"
                    )
                return state, False
            if state["state_revision"] != expected_revision:
                raise IntegrityError(
                    f"Stale state revision: expected {expected_revision}, "
                    f"current {state['state_revision']}"
                )
            updated = copy.deepcopy(state)
            mutator(updated)
            updated["state_revision"] += 1
            updated["operations"].append(
                {
                    "id": effective_operation_id,
                    "kind": operation_kind,
                    "recorded_at": utc_now(),
                }
            )
            updated["operations"] = updated["operations"][-200:]
            validate_state(updated)
            atomic_write_json(path, updated)
            return updated, True

    def mutate_with_immutable_artifact(
        self,
        change_id: str,
        *,
        expected_revision: int,
        operation_id: str | None,
        operation_kind: str,
        artifact_path: Path,
        artifact_value: dict[str, Any],
        mutator: Callable[[dict[str, Any]], None],
    ) -> tuple[dict[str, Any], bool]:
        """Commit an immutable artifact and its state reference under one state lock.

        A retry may finish a transaction interrupted after the artifact write. If
        the state write fails in-process, a newly-created artifact is removed.
        """
        path = self.path(change_id)
        lock_path = path.with_suffix(path.suffix + ".lock")
        effective_operation_id = operation_id or str(uuid.uuid4())
        with FileLock(lock_path):
            state = self.load(change_id)
            existing_operation = next(
                (
                    operation
                    for operation in state["operations"]
                    if isinstance(operation, dict)
                    and operation.get("id") == effective_operation_id
                ),
                None,
            )
            if existing_operation:
                if existing_operation.get("kind") != operation_kind:
                    raise IntegrityError(
                        f"Operation ID already belongs to {existing_operation.get('kind')}: "
                        f"{effective_operation_id}"
                    )
                return state, False
            if state["state_revision"] != expected_revision:
                raise IntegrityError(
                    f"Stale state revision: expected {expected_revision}, "
                    f"current {state['state_revision']}"
                )
            created_artifact = False
            if artifact_path.exists():
                if read_json(artifact_path) != artifact_value:
                    raise IntegrityError(
                        "Immutable artifact already exists with different content: "
                        f"{artifact_path}"
                    )
            else:
                atomic_write_json(artifact_path, artifact_value, backup=False)
                created_artifact = True
            try:
                updated = copy.deepcopy(state)
                mutator(updated)
                updated["state_revision"] += 1
                updated["operations"].append(
                    {
                        "id": effective_operation_id,
                        "kind": operation_kind,
                        "recorded_at": utc_now(),
                    }
                )
                updated["operations"] = updated["operations"][-200:]
                validate_state(updated)
                atomic_write_json(path, updated)
            except Exception:
                if created_artifact:
                    artifact_path.unlink(missing_ok=True)
                raise
            return updated, True


def current_definition_digest(root: Path, state: dict[str, Any]) -> str:
    return package_digest(root, state["artifacts"])


def derived_approval_statuses(root: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    definition_digest = current_definition_digest(root, state)
    head = git_head(root)
    approvals: list[dict[str, Any]] = []
    for approval in state["approvals"]:
        item = copy.deepcopy(approval)
        if item.get("status") != "current":
            approvals.append(item)
            continue
        decision = item.get("decision")
        if decision in {"definition", "design", "architecture", "exception"} and (
            item.get("object_digest") != definition_digest
        ):
            item["status"] = "stale"
            item["stale_reason"] = "authored-content-digest-changed"
        if decision == "accept" and item.get("git_sha") != head:
            item["status"] = "stale"
            item["stale_reason"] = "git-head-changed"
        approvals.append(item)
    return approvals
