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
        return self.mutate_with_immutable_artifacts(
            change_id,
            expected_revision=expected_revision,
            operation_id=operation_id,
            operation_kind=operation_kind,
            artifacts=[(artifact_path, artifact_value)],
            mutator=mutator,
        )

    def mutate_with_immutable_artifacts(
        self,
        change_id: str,
        *,
        expected_revision: int,
        operation_id: str | None,
        operation_kind: str,
        artifacts: list[tuple[Path, dict[str, Any]]],
        mutator: Callable[[dict[str, Any]], None],
    ) -> tuple[dict[str, Any], bool]:
        """Commit an immutable artifact and its state reference under one state lock.

        A retry may finish a transaction interrupted after artifact writes. If
        the state write fails in-process, every newly-created artifact is removed.
        """
        if not artifacts:
            raise IntegrityError("Immutable state mutation requires at least one artifact")
        artifact_paths = [artifact_path.resolve() for artifact_path, _ in artifacts]
        if len(artifact_paths) != len(set(artifact_paths)):
            raise IntegrityError("Immutable state mutation contains duplicate artifact paths")
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
                for artifact_path, artifact_value in artifacts:
                    if not artifact_path.is_file() or read_json(artifact_path) != artifact_value:
                        raise IntegrityError(
                            "Completed immutable operation has a missing or changed artifact: "
                            f"{artifact_path}"
                        )
                return state, False
            if state["state_revision"] != expected_revision:
                raise IntegrityError(
                    f"Stale state revision: expected {expected_revision}, "
                    f"current {state['state_revision']}"
                )
            created_artifacts: list[Path] = []
            try:
                for artifact_path, artifact_value in artifacts:
                    if artifact_path.exists():
                        if read_json(artifact_path) != artifact_value:
                            raise IntegrityError(
                                "Immutable artifact already exists with different content: "
                                f"{artifact_path}"
                            )
                    else:
                        atomic_write_json(artifact_path, artifact_value, backup=False)
                        created_artifacts.append(artifact_path)
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
                for created_artifact in reversed(created_artifacts):
                    created_artifact.unlink(missing_ok=True)
                raise
            return updated, True

    def claim_review_lane(
        self,
        change_id: str,
        *,
        attempt: dict[str, Any],
        operation_kind: str,
        max_attempts: int = 2,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Atomically claim one review lane without relying on a stale revision.

        Exactly one running attempt is allowed for a review/lane key. Callers may
        safely race: the winner records ``running`` and every loser receives that
        same immutable attempt instead of launching another model process.
        """
        path = self.path(change_id)
        lock_path = path.with_suffix(path.suffix + ".lock")
        review_id = attempt.get("review_id")
        lane_key = attempt.get("lane_key")
        attempt_id = attempt.get("attempt_id")
        operation_id = attempt.get("operation_id")
        if not all(
            isinstance(value, str) and value
            for value in (review_id, lane_key, attempt_id, operation_id)
        ):
            raise IntegrityError("Review lane claim requires stable identifiers")
        with FileLock(lock_path):
            state = self.load(change_id)
            existing_operation = next(
                (
                    operation
                    for operation in state["operations"]
                    if isinstance(operation, dict)
                    and operation.get("id") == operation_id
                ),
                None,
            )
            if existing_operation and existing_operation.get("kind") != operation_kind:
                raise IntegrityError(
                    f"Operation ID already belongs to {existing_operation.get('kind')}: "
                    f"{operation_id}"
                )
            attempts = [
                item
                for item in state["reviews"]
                if isinstance(item, dict)
                and item.get("review_id") == review_id
                and item.get("lane_key") == lane_key
            ]
            contract_digest = attempt.get("lane_contract_digest")
            contract_attempts = (
                [
                    item
                    for item in attempts
                    if item.get("lane_contract_digest") == contract_digest
                ]
                if isinstance(contract_digest, str) and contract_digest
                else attempts
            )
            matching = next(
                (item for item in attempts if item.get("attempt_id") == attempt_id),
                None,
            )
            if matching:
                return state, matching, False
            running = next(
                (
                    item
                    for item in reversed(attempts)
                    if item.get("status") == "running"
                ),
                None,
            )
            if running:
                return state, running, False
            if len(contract_attempts) >= max_attempts:
                return state, contract_attempts[-1], False
            updated = copy.deepcopy(state)
            recorded_attempt = copy.deepcopy(attempt)
            recorded_attempt["status"] = "running"
            recorded_attempt.setdefault("started_at", utc_now())
            updated["reviews"].append(recorded_attempt)
            if existing_operation:
                for operation in updated["operations"]:
                    if (
                        isinstance(operation, dict)
                        and operation.get("id") == operation_id
                    ):
                        operation["status"] = "running"
                        operation["attempt_id"] = attempt_id
                        break
            else:
                updated["operations"].append(
                    {
                        "id": operation_id,
                        "kind": operation_kind,
                        "status": "running",
                        "attempt_id": attempt_id,
                        "recorded_at": utc_now(),
                    }
                )
            updated["operations"] = updated["operations"][-200:]
            updated["state_revision"] += 1
            validate_state(updated)
            atomic_write_json(path, updated)
            return updated, recorded_attempt, True

    def finish_review_lane(
        self,
        change_id: str,
        *,
        attempt_id: str,
        expected_status: str,
        updates: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """CAS-finalize one previously claimed review lane."""
        path = self.path(change_id)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with FileLock(lock_path):
            state = self.load(change_id)
            index = next(
                (
                    index
                    for index, item in enumerate(state["reviews"])
                    if isinstance(item, dict)
                    and item.get("attempt_id") == attempt_id
                ),
                None,
            )
            if index is None:
                raise IntegrityError(f"Unknown review lane attempt: {attempt_id}")
            current = state["reviews"][index]
            if current.get("status") != expected_status:
                terminal = copy.deepcopy(current)
                if all(terminal.get(key) == value for key, value in updates.items()):
                    return state, terminal, False
                raise IntegrityError(
                    f"Review lane attempt status changed: {attempt_id}; "
                    f"expected={expected_status}, current={current.get('status')}"
                )
            updated = copy.deepcopy(state)
            recorded = updated["reviews"][index]
            recorded.update(copy.deepcopy(updates))
            operation_id = recorded.get("operation_id")
            for operation in updated["operations"]:
                if (
                    isinstance(operation, dict)
                    and operation.get("id") == operation_id
                ):
                    operation["status"] = recorded.get("status")
                    operation["completed_at"] = recorded.get(
                        "completed_at",
                        utc_now(),
                    )
                    break
            updated["state_revision"] += 1
            validate_state(updated)
            atomic_write_json(path, updated)
            return updated, copy.deepcopy(recorded), True

    def update_review_pipeline(
        self,
        change_id: str,
        *,
        review_id: str,
        operation_id: str,
        updates: dict[str, Any],
        create: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Create or update one review-run pipeline record under the state lock.

        Pipeline progress is deliberately separate from lane attempts. A lane can
        complete successfully while deterministic assembly or import still fails;
        recording that boundary prevents ``review-status`` from incorrectly
        reporting the pack as merely ready to start again.
        """
        if not review_id or not operation_id:
            raise IntegrityError("Review pipeline requires stable identifiers")
        path = self.path(change_id)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with FileLock(lock_path):
            state = self.load(change_id)
            index = next(
                (
                    index
                    for index, item in enumerate(state["reviews"])
                    if isinstance(item, dict)
                    and item.get("kind") == "pipeline"
                    and item.get("review_id") == review_id
                    and item.get("operation_id") == operation_id
                ),
                None,
            )
            if index is None and not create:
                raise IntegrityError(
                    f"Unknown review pipeline: {review_id}/{operation_id}"
                )
            updated = copy.deepcopy(state)
            if index is None:
                record = {
                    "kind": "pipeline",
                    "review_id": review_id,
                    "operation_id": operation_id,
                    "status": "running",
                    "stage": "preparing",
                    "started_at": utc_now(),
                }
                record.update(copy.deepcopy(updates))
                updated["reviews"].append(record)
                recorded = updated["reviews"][-1]
            else:
                recorded = updated["reviews"][index]
                if all(recorded.get(key) == value for key, value in updates.items()):
                    return state, copy.deepcopy(recorded), False
                recorded.update(copy.deepcopy(updates))
            pipeline_operation_id = f"{operation_id}:pipeline"
            operation = next(
                (
                    item
                    for item in updated["operations"]
                    if isinstance(item, dict)
                    and item.get("id") == pipeline_operation_id
                ),
                None,
            )
            if operation is None:
                updated["operations"].append(
                    {
                        "id": pipeline_operation_id,
                        "kind": "review-run-pipeline",
                        "recorded_at": utc_now(),
                    }
                )
                operation = updated["operations"][-1]
            operation["status"] = recorded.get("status")
            operation["stage"] = recorded.get("stage")
            if recorded.get("status") in {
                "completed",
                "failed",
                "failed-finalize",
            }:
                operation["completed_at"] = recorded.get(
                    "completed_at",
                    utc_now(),
                )
            updated["operations"] = updated["operations"][-200:]
            updated["state_revision"] += 1
            validate_state(updated)
            atomic_write_json(path, updated)
            return updated, copy.deepcopy(recorded), True


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
