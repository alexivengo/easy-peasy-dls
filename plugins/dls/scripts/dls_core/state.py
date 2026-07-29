"""Versioned DLS state and approval integrity."""

from __future__ import annotations

import copy
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable

from .errors import IntegrityError, UsageError
from .io import FileLock, atomic_write_json, read_json, sha256_bytes, utc_now
from .repo import (
    git_head,
    git_product_tree_digest,
    git_source_dirty_paths,
    package_digest,
    run_git,
)

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
DEPENDENCY_STAGES = {"definition", "implementation", "review", "acceptance"}
DEPENDENCY_REQUIREMENTS = {
    "definition-approved",
    "review-clear",
    "accepted",
    "accepted-in-base",
}


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
        "candidate_runs": [],
        "finding_dispositions": [],
        "evidence": [],
        "operations": [
            {
                "id": operation_id,
                "kind": "state-create",
                "recorded_at": utc_now(),
            }
        ],
        "dependencies": [],
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
    if "candidate_runs" in state and not isinstance(state["candidate_runs"], list):
        raise IntegrityError("state.candidate_runs must be list")
    dependencies = state.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise IntegrityError("state.dependencies must be list")
    if len(dependencies) > 64:
        raise IntegrityError("state.dependencies exceeds 64 entries")
    targets: set[str] = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise IntegrityError("state.dependencies entries must be objects")
        target = dependency.get("change_id")
        if not isinstance(target, str):
            raise IntegrityError("Dependency change_id must be a string")
        validate_change_id(target)
        if target == change_id:
            raise IntegrityError("A change cannot depend on itself")
        if target in targets:
            raise IntegrityError(f"Duplicate dependency target: {target}")
        targets.add(target)
        if dependency.get("blocks_stage") not in DEPENDENCY_STAGES:
            raise IntegrityError(
                f"Invalid dependency blocks_stage: {dependency.get('blocks_stage')!r}"
            )
        if dependency.get("requires") not in DEPENDENCY_REQUIREMENTS:
            raise IntegrityError(
                f"Invalid dependency requires: {dependency.get('requires')!r}"
            )
        target_digest = dependency.get("target_definition_digest")
        if not isinstance(target_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", target_digest
        ):
            raise IntegrityError("Dependency target_definition_digest must be SHA-256")
        rationale = dependency.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 1000:
            raise IntegrityError("Dependency rationale must contain 1-1000 characters")


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

    def claim_review_finalization(
        self,
        change_id: str,
        *,
        review_id: str,
        finalization_id: str,
        runner_pid: int,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Claim the one deterministic assembly/import boundary for a review."""
        path = self.path(change_id)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with FileLock(lock_path):
            state = self.load(change_id)
            result = next(
                (
                    item
                    for item in reversed(state["reviews"])
                    if isinstance(item, dict)
                    and item.get("kind") == "result"
                    and item.get("review_id") == review_id
                ),
                None,
            )
            if result is not None:
                return state, {
                    "kind": "finalization",
                    "review_id": review_id,
                    "finalization_id": finalization_id,
                    "status": "completed",
                    "result_path": result.get("result_path"),
                }, False
            index = next(
                (
                    index
                    for index, item in enumerate(state["reviews"])
                    if isinstance(item, dict)
                    and item.get("kind") == "finalization"
                    and item.get("review_id") == review_id
                ),
                None,
            )
            if index is not None and state["reviews"][index].get("status") == "running":
                runner_pid_value = state["reviews"][index].get("runner_pid")
                alive = False
                if isinstance(runner_pid_value, int) and runner_pid_value > 0:
                    try:
                        os.kill(runner_pid_value, 0)
                        alive = True
                    except OSError:
                        alive = False
                if alive:
                    return state, copy.deepcopy(state["reviews"][index]), False
            updated = copy.deepcopy(state)
            record = {
                "kind": "finalization",
                "review_id": review_id,
                "finalization_id": finalization_id,
                "status": "running",
                "runner_pid": runner_pid,
                "started_at": utc_now(),
            }
            if index is None:
                updated["reviews"].append(record)
                recorded = updated["reviews"][-1]
            else:
                updated["reviews"][index] = record
                recorded = updated["reviews"][index]
            updated["state_revision"] += 1
            validate_state(updated)
            atomic_write_json(path, updated)
            return updated, copy.deepcopy(recorded), True

    def finish_review_finalization(
        self,
        change_id: str,
        *,
        review_id: str,
        finalization_id: str,
        status: str,
        error: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        if status not in {"completed", "failed"}:
            raise IntegrityError("Review finalization status is invalid")
        path = self.path(change_id)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with FileLock(lock_path):
            state = self.load(change_id)
            index = next(
                (
                    index
                    for index, item in enumerate(state["reviews"])
                    if isinstance(item, dict)
                    and item.get("kind") == "finalization"
                    and item.get("review_id") == review_id
                    and item.get("finalization_id") == finalization_id
                ),
                None,
            )
            if index is None:
                raise IntegrityError("Unknown review finalization claim")
            current = state["reviews"][index]
            if current.get("status") == status:
                return state, copy.deepcopy(current), False
            if current.get("status") != "running":
                raise IntegrityError("Review finalization claim is not running")
            updated = copy.deepcopy(state)
            recorded = updated["reviews"][index]
            recorded["status"] = status
            recorded["completed_at"] = utc_now()
            if error is not None:
                recorded["error"] = error
            updated["state_revision"] += 1
            validate_state(updated)
            atomic_write_json(path, updated)
            return updated, copy.deepcopy(recorded), True

    def claim_candidate_run(
        self,
        change_id: str,
        *,
        candidate_run: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Claim or resume one exact candidate contract under the state lock."""
        run_id = candidate_run.get("run_id")
        contract_digest = candidate_run.get("contract_digest")
        if not isinstance(run_id, str) or not isinstance(contract_digest, str):
            raise IntegrityError("Candidate run requires stable identifiers")
        path = self.path(change_id)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with FileLock(lock_path):
            state = self.load(change_id)
            runs = state.get("candidate_runs", [])
            operation_id = candidate_run.get("operation_id")
            operation_conflict = next(
                (
                    item
                    for item in runs
                    if isinstance(item, dict)
                    and isinstance(operation_id, str)
                    and item.get("operation_id") == operation_id
                    and item.get("run_id") != run_id
                ),
                None,
            )
            if operation_conflict is not None:
                raise IntegrityError(
                    "Candidate operation ID already belongs to another candidate contract"
                )
            running = next(
                (
                    item
                    for item in reversed(runs)
                    if isinstance(item, dict) and item.get("status") == "running"
                ),
                None,
            )
            if running is not None and running.get("run_id") != run_id:
                return state, copy.deepcopy(running), False
            existing_index = next(
                (
                    index
                    for index, item in enumerate(runs)
                    if isinstance(item, dict) and item.get("run_id") == run_id
                ),
                None,
            )
            if existing_index is not None:
                existing = runs[existing_index]
                if existing.get("contract_digest") != contract_digest:
                    raise IntegrityError("Candidate run contract digest changed")
                if existing.get("status") in {"running", "completed"}:
                    return state, copy.deepcopy(existing), False
            updated = copy.deepcopy(state)
            updated_runs = updated.setdefault("candidate_runs", [])
            if existing_index is None:
                recorded = copy.deepcopy(candidate_run)
                recorded["status"] = "running"
                recorded.setdefault("started_at", utc_now())
                recorded["updated_at"] = utc_now()
                updated_runs.append(recorded)
            else:
                recorded = updated_runs[existing_index]
                recorded.update(
                    {
                        "status": "running",
                        "phase": "preflight",
                        "active_command": None,
                        "runner_pid": candidate_run.get("runner_pid"),
                        "operation_id": candidate_run.get("operation_id"),
                        "updated_at": utc_now(),
                    }
                )
                recorded.pop("completed_at", None)
                recorded.pop("failure_reason", None)
                recorded.pop("failed_command", None)
                recorded.pop("next_action", None)
            updated["candidate_runs"] = updated_runs[-50:]
            updated["state_revision"] += 1
            validate_state(updated)
            atomic_write_json(path, updated)
            return updated, copy.deepcopy(recorded), True

    def update_candidate_run(
        self,
        change_id: str,
        *,
        run_id: str,
        updates: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """CAS-free progress update for the process that owns one candidate run."""
        path = self.path(change_id)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with FileLock(lock_path):
            state = self.load(change_id)
            runs = state.get("candidate_runs", [])
            index = next(
                (
                    index
                    for index, item in enumerate(runs)
                    if isinstance(item, dict) and item.get("run_id") == run_id
                ),
                None,
            )
            if index is None:
                raise IntegrityError(f"Unknown candidate run: {run_id}")
            current = runs[index]
            if all(current.get(key) == value for key, value in updates.items()):
                return state, copy.deepcopy(current), False
            updated = copy.deepcopy(state)
            recorded = updated.setdefault("candidate_runs", [])[index]
            recorded.update(copy.deepcopy(updates))
            recorded["updated_at"] = utc_now()
            updated["state_revision"] += 1
            validate_state(updated)
            atomic_write_json(path, updated)
            return updated, copy.deepcopy(recorded), True


def current_definition_digest(root: Path, state: dict[str, Any]) -> str:
    artifact_digest = package_digest(root, state["artifacts"])
    dependencies = state.get("dependencies", [])
    if not dependencies:
        # Preserve every legacy approval digest byte-for-byte.
        return artifact_digest
    normalized = [
        {
            "change_id": item["change_id"],
            "blocks_stage": item["blocks_stage"],
            "requires": item["requires"],
            "target_definition_digest": item["target_definition_digest"],
            "rationale": item["rationale"].strip(),
        }
        for item in sorted(dependencies, key=lambda value: value["change_id"])
    ]
    return sha256_bytes(
        json.dumps(
            {
                "contract": "dls-change-dependencies/v1",
                "artifact_digest": artifact_digest,
                "dependencies": normalized,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def derived_approval_statuses(root: Path, state: dict[str, Any]) -> list[dict[str, Any]]:
    definition_digest = current_definition_digest(root, state)
    head = git_head(root)
    approvals: list[dict[str, Any]] = []
    dependency_drift: list[str] = []
    if state.get("dependencies"):
        try:
            from .dependencies import dependency_snapshot_drift

            dependency_drift = dependency_snapshot_drift(root, state)
        except (IntegrityError, UsageError):
            dependency_drift = ["dependency-state-unavailable"]
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
        elif decision in {"definition", "design", "architecture"} and dependency_drift:
            item["status"] = "stale"
            item["stale_reason"] = "dependency-definition-digest-changed"
        if decision == "accept":
            accepted_sha = item.get("git_sha")
            if item.get("object_digest") != definition_digest:
                item["status"] = "stale"
                item["stale_reason"] = "authored-content-digest-changed"
            elif (
                accepted_sha is None
                and head is None
                and state.get("control_level") == "routine"
            ):
                # Preserve the existing non-Git routine path.  Exact-revision
                # proof remains mandatory for standard and critical work.
                pass
            elif not isinstance(accepted_sha, str) or not accepted_sha or not head:
                item["status"] = "stale"
                item["stale_reason"] = "accepted-revision-unavailable"
            elif run_git(
                root,
                "merge-base",
                "--is-ancestor",
                accepted_sha,
                head,
                check=False,
            ).returncode != 0:
                item["status"] = "stale"
                item["stale_reason"] = "git-history-diverged"
            else:
                accepted_source_digest = item.get("source_digest")
                if not isinstance(accepted_source_digest, str):
                    # Legacy state v1 approvals did not persist this field.
                    accepted_source_digest = git_product_tree_digest(root, accepted_sha)
                current_source_digest = git_product_tree_digest(root, head)
                if not accepted_source_digest or not current_source_digest:
                    item["status"] = "stale"
                    item["stale_reason"] = "product-source-unavailable"
                elif accepted_source_digest != current_source_digest:
                    item["status"] = "stale"
                    item["stale_reason"] = "product-source-changed"
                elif git_source_dirty_paths(root):
                    item["status"] = "stale"
                    item["stale_reason"] = "product-source-dirty"
        approvals.append(item)
    return approvals
