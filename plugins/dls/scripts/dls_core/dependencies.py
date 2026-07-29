"""Stage-aware dependencies between DLS changes in one Git repository."""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from typing import Any

from .errors import IntegrityError, UsageError
from .io import sha256_bytes, utc_now
from .repo import git_head, run_git
from .state import (
    DEPENDENCY_REQUIREMENTS,
    DEPENDENCY_STAGES,
    StateStore,
    current_definition_digest,
    derived_approval_statuses,
    validate_change_id,
)
from .worktrees import git_common_dir, resolve_change_root

DEPENDENCY_CONTRACT = "dls-change-dependencies/v1"
DEPENDENCY_EXCEPTION_CONTRACT = "dls-dependency-exception/v1"
MAX_DEPENDENCY_DEPTH = 16
STAGE_ORDER = {
    "definition": 0,
    "implementation": 1,
    "review": 2,
    "acceptance": 3,
}


def _normalized(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "change_id": item["change_id"],
            "blocks_stage": item["blocks_stage"],
            "requires": item["requires"],
            "target_definition_digest": item["target_definition_digest"],
            "rationale": item["rationale"].strip(),
        }
        for item in sorted(items, key=lambda value: value["change_id"])
    ]


def dependency_contract_digest(state: dict[str, Any]) -> str:
    return sha256_bytes(
        json.dumps(
            {
                "contract": DEPENDENCY_CONTRACT,
                "dependencies": _normalized(state.get("dependencies", [])),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def dependency_record_digest(item: dict[str, Any]) -> str:
    return sha256_bytes(
        json.dumps(
            {"contract": DEPENDENCY_CONTRACT, **_normalized([item])[0]},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _target(root: Path, change_id: str) -> tuple[Path, dict[str, Any]]:
    owner = resolve_change_root(root, change_id)
    if git_common_dir(owner) != git_common_dir(root):
        raise IntegrityError(f"Dependency target belongs to another repository: {change_id}")
    return owner, StateStore(owner).load(change_id)


def _assert_acyclic(
    root: Path,
    *,
    source_change_id: str,
    proposed: list[dict[str, Any]],
) -> None:
    stack: list[tuple[str, int]] = [
        (item["change_id"], 1) for item in proposed
    ]
    visited: set[str] = set()
    while stack:
        change_id, depth = stack.pop()
        if change_id == source_change_id:
            raise IntegrityError(f"Dependency cycle includes {source_change_id}")
        if depth > MAX_DEPENDENCY_DEPTH:
            raise IntegrityError(
                f"Dependency graph exceeds depth {MAX_DEPENDENCY_DEPTH}"
            )
        if change_id in visited:
            continue
        visited.add(change_id)
        _, state = _target(root, change_id)
        for item in state.get("dependencies", []):
            stack.append((item["change_id"], depth + 1))


def dependency_set(
    root: Path,
    *,
    change_id: str,
    target_change_id: str,
    blocks_stage: str,
    requires: str,
    rationale: str,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    change_id = validate_change_id(change_id)
    target_change_id = validate_change_id(target_change_id)
    if change_id == target_change_id:
        raise UsageError("A change cannot depend on itself")
    if blocks_stage not in DEPENDENCY_STAGES:
        raise UsageError(f"Unsupported dependency stage: {blocks_stage}")
    if requires not in DEPENDENCY_REQUIREMENTS:
        raise UsageError(f"Unsupported dependency requirement: {requires}")
    rationale = rationale.strip()
    if not rationale or len(rationale) > 1000:
        raise UsageError("Dependency rationale must contain 1-1000 characters")
    owner = resolve_change_root(root, change_id)
    target_owner, target_state = _target(owner, target_change_id)
    if git_common_dir(target_owner) != git_common_dir(owner):
        raise IntegrityError("Cross-repository dependencies are not supported")
    store = StateStore(owner)
    state = store.load(change_id)
    target_digest = current_definition_digest(target_owner, target_state)
    record = {
        "change_id": target_change_id,
        "blocks_stage": blocks_stage,
        "requires": requires,
        "target_definition_digest": target_digest,
        "rationale": rationale,
        "recorded_at": utc_now(),
    }
    existing = next(
        (
            item
            for item in state.get("dependencies", [])
            if item.get("change_id") == target_change_id
        ),
        None,
    )
    stable_keys = (
        "change_id",
        "blocks_stage",
        "requires",
        "target_definition_digest",
        "rationale",
    )
    if existing and all(existing.get(key) == record.get(key) for key in stable_keys):
        return {
            "ok": True,
            "dry_run": dry_run,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "dependency": copy.deepcopy(existing),
            "dependency_digest": dependency_contract_digest(state),
        }
    proposed = [
        item
        for item in state.get("dependencies", [])
        if item.get("change_id") != target_change_id
    ]
    proposed.append(record)
    _assert_acyclic(owner, source_change_id=change_id, proposed=proposed)
    projected = copy.deepcopy(state)
    projected["dependencies"] = proposed
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "dependency": record,
            "dependency_digest": dependency_contract_digest(projected),
        }

    def mutate(value: dict[str, Any]) -> None:
        value["dependencies"] = copy.deepcopy(proposed)

    updated, changed = store.mutate(
        change_id,
        expected_revision=state["state_revision"],
        operation_id=operation_id or str(uuid.uuid4()),
        operation_kind=(
            f"dependency-set:{target_change_id}:{dependency_record_digest(record)}"
        ),
        mutator=mutate,
    )
    recorded = next(
        item
        for item in updated["dependencies"]
        if item["change_id"] == target_change_id
    )
    return {
        "ok": True,
        "dry_run": False,
        "changed": changed,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "dependency": recorded,
        "dependency_digest": dependency_contract_digest(updated),
    }


def dependency_remove(
    root: Path,
    *,
    change_id: str,
    target_change_id: str,
    operation_id: str | None,
    dry_run: bool = False,
) -> dict[str, Any]:
    owner = resolve_change_root(root, validate_change_id(change_id))
    target_change_id = validate_change_id(target_change_id)
    store = StateStore(owner)
    state = store.load(change_id)
    proposed = [
        item
        for item in state.get("dependencies", [])
        if item.get("change_id") != target_change_id
    ]
    changed = len(proposed) != len(state.get("dependencies", []))
    if not changed or dry_run:
        projected = copy.deepcopy(state)
        projected["dependencies"] = proposed
        return {
            "ok": True,
            "dry_run": dry_run,
            "changed": False,
            "change_id": change_id,
            "state_revision": state["state_revision"],
            "dependency_digest": dependency_contract_digest(projected),
        }

    def mutate(value: dict[str, Any]) -> None:
        value["dependencies"] = copy.deepcopy(proposed)

    updated, mutated = store.mutate(
        change_id,
        expected_revision=state["state_revision"],
        operation_id=operation_id or str(uuid.uuid4()),
        operation_kind=f"dependency-remove:{target_change_id}",
        mutator=mutate,
    )
    return {
        "ok": True,
        "dry_run": False,
        "changed": mutated,
        "change_id": change_id,
        "state_revision": updated["state_revision"],
        "dependency_digest": dependency_contract_digest(updated),
    }


def _latest_review_result(state: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in reversed(state.get("reviews", []))
            if isinstance(item, dict) and item.get("kind") == "result"
        ),
        None,
    )


def _current_acceptance(root: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in reversed(derived_approval_statuses(root, state))
            if item.get("decision") == "accept" and item.get("status") == "current"
        ),
        None,
    )


def _exception_matches(
    root: Path,
    state: dict[str, Any],
    *,
    item: dict[str, Any],
    target_head: str,
    dependent_head: str,
) -> bool:
    expected = {
        "contract": DEPENDENCY_EXCEPTION_CONTRACT,
        "dependency_digest": dependency_record_digest(item),
        "target_head_sha": target_head,
        "dependent_head_sha": dependent_head,
    }
    for approval in derived_approval_statuses(root, state):
        if approval.get("decision") != "exception" or approval.get("status") != "current":
            continue
        conditions = approval.get("conditions")
        if not isinstance(conditions, str):
            continue
        try:
            parsed = json.loads(conditions)
        except json.JSONDecodeError:
            continue
        if parsed == expected:
            return True
    return False


def _requirement_status(
    root: Path,
    state: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    target_id = item["change_id"]
    try:
        target_owner, target_state = _target(root, target_id)
    except (IntegrityError, UsageError) as exc:
        return {
            "satisfied": False,
            "reason": "target-unavailable",
            "detail": str(exc),
            "target_head_sha": None,
        }
    target_definition = current_definition_digest(target_owner, target_state)
    if target_definition != item["target_definition_digest"]:
        return {
            "satisfied": False,
            "reason": "target-definition-drift",
            "detail": f"expected={item['target_definition_digest']}; current={target_definition}",
            "target_head_sha": git_head(target_owner),
        }
    requirement = item["requires"]
    target_owner_head = git_head(target_owner)
    approvals = derived_approval_statuses(target_owner, target_state)
    definition_approved = any(
        approval.get("decision") == "definition" and approval.get("status") == "current"
        for approval in approvals
    )
    acceptance = _current_acceptance(target_owner, target_state)
    latest_review = _latest_review_result(target_state)
    review_clear = bool(
        latest_review
        and latest_review.get("verdict") == "review-clear"
        and (
            latest_review.get("head_sha") == target_owner_head
            or (
                acceptance is not None
                and latest_review.get("head_sha") == acceptance.get("git_sha")
            )
        )
    )
    accepted_head = acceptance.get("git_sha") if acceptance is not None else None
    accepted = isinstance(accepted_head, str) and bool(accepted_head)
    if requirement == "definition-approved":
        satisfied = definition_approved
    elif requirement == "review-clear":
        satisfied = review_clear
    elif requirement == "accepted":
        satisfied = accepted
    else:
        dependent_head = git_head(root)
        contained = bool(
            accepted
            and isinstance(accepted_head, str)
            and isinstance(dependent_head, str)
            and run_git(
                root,
                "merge-base",
                "--is-ancestor",
                accepted_head,
                dependent_head,
                check=False,
            ).returncode
            == 0
        )
        satisfied = contained or bool(
            accepted
            and isinstance(accepted_head, str)
            and isinstance(dependent_head, str)
            and _exception_matches(
                root,
                state,
                item=item,
                target_head=accepted_head,
                dependent_head=dependent_head,
            )
        )
        if accepted and not satisfied:
            return {
                "satisfied": False,
                "reason": "accepted-not-in-base",
                "detail": (
                    f"accepted_head={accepted_head}; dependent_head={dependent_head}"
                ),
                "target_head_sha": accepted_head,
                "target_owner_head_sha": target_owner_head,
            }
    reported_target_head = (
        accepted_head
        if requirement in {"accepted", "accepted-in-base"} and accepted
        else target_owner_head
    )
    return {
        "satisfied": satisfied,
        "reason": None if satisfied else f"requires-{requirement}",
        "detail": requirement,
        "target_head_sha": reported_target_head,
        "target_owner_head_sha": target_owner_head,
    }


def dependency_projection(
    root: Path,
    state: dict[str, Any],
    *,
    stage: str | None = None,
) -> dict[str, Any]:
    if stage is not None and stage not in STAGE_ORDER:
        raise UsageError(f"Unsupported dependency stage: {stage}")
    items: list[dict[str, Any]] = []
    for dependency in _normalized(state.get("dependencies", [])):
        status = _requirement_status(root, state, dependency)
        applies = bool(
            stage is None
            or STAGE_ORDER[stage] >= STAGE_ORDER[dependency["blocks_stage"]]
        )
        items.append(
            {
                **dependency,
                "dependency_digest": dependency_record_digest(dependency),
                "applies": applies,
                **status,
            }
        )
    blocked = [item for item in items if item["applies"] and not item["satisfied"]]
    if not blocked:
        next_action = None
    elif any(item["reason"] == "accepted-not-in-base" for item in blocked):
        next_action = {
            "id": "rebase-after-dependency",
            "detail": ",".join(item["change_id"] for item in blocked),
        }
    else:
        next_action = {
            "id": "wait-dependency",
            "detail": ",".join(item["change_id"] for item in blocked),
        }
    return {
        "contract": DEPENDENCY_CONTRACT,
        "digest": dependency_contract_digest(state),
        "stage": stage,
        "satisfied": not blocked,
        "items": items,
        "blocked_count": len(blocked),
        "next_action": next_action,
    }


def dependency_snapshot_drift(root: Path, state: dict[str, Any]) -> list[str]:
    drifted: list[str] = []
    for item in state.get("dependencies", []):
        try:
            owner, target_state = _target(root, item["change_id"])
            current = current_definition_digest(owner, target_state)
        except (IntegrityError, UsageError):
            drifted.append(item["change_id"])
            continue
        if current != item.get("target_definition_digest"):
            drifted.append(item["change_id"])
    return sorted(set(drifted))


def dependency_list(root: Path, *, change_id: str) -> dict[str, Any]:
    owner = resolve_change_root(root, validate_change_id(change_id))
    state = StateStore(owner).load(change_id)
    projection = dependency_projection(owner, state)
    return {
        "ok": True,
        "change_id": change_id,
        "state_revision": state["state_revision"],
        "dependencies": projection["items"],
        "dependency_digest": projection["digest"],
    }
