"""Derived parallel-delivery readiness and overlap projections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .dependencies import dependency_projection
from .errors import IntegrityError
from .io import sha256_bytes
from .repo import git_changed_files, git_head, git_source_dirty_paths, is_git_repo, run_git
from .state import StateStore, current_definition_digest, derived_approval_statuses
from .worktrees import resolve_change_root, worktree_list

DELIVERY_MAP_CONTRACT = "dls-delivery-map/v1"
OVERLAP_CONTRACT = "dls-change-overlap/v1"
DELIVERY_MAP_LIMIT = 64
DELIVERY_MAP_MAX_BYTES = 16 * 1024


def _entry_key(entry: dict[str, Any]) -> tuple[str, str]:
    return str(entry.get("registered_at") or ""), str(entry.get("change_id") or "")


def _changed_paths(entry: dict[str, Any]) -> tuple[set[str], str | None]:
    if not entry.get("valid"):
        return set(), "invalid-worktree"
    base_sha = entry.get("base_sha")
    if not isinstance(base_sha, str):
        return set(), "base-unavailable"
    owner = Path(entry["owner_root"])
    head = git_head(owner)
    if not head:
        return set(), "head-unavailable"
    paths = {
        path
        for path in git_changed_files(owner, base_sha, head)
        if path != ".dls" and not path.startswith(".dls/")
    }
    paths.update(git_source_dirty_paths(owner))
    return paths, None


def _accepted_in_base(owner: Path, predecessor: dict[str, Any]) -> bool:
    predecessor_owner = Path(predecessor["owner_root"])
    predecessor_state = StateStore(predecessor_owner).load(predecessor["change_id"])
    predecessor_head = git_head(predecessor_owner)
    accepted = any(
        approval.get("decision") == "accept"
        and approval.get("status") == "current"
        and approval.get("git_sha") == predecessor_head
        for approval in derived_approval_statuses(
            predecessor_owner, predecessor_state
        )
    )
    dependent_head = git_head(owner)
    return bool(
        accepted
        and predecessor_head
        and dependent_head
        and run_git(
            owner,
            "merge-base",
            "--is-ancestor",
            predecessor_head,
            dependent_head,
            check=False,
        ).returncode
        == 0
    )


def overlap_projection(
    root: Path,
    *,
    change_id: str,
    verbose: bool = False,
) -> dict[str, Any]:
    owner = resolve_change_root(root, change_id)
    if not is_git_repo(owner):
        return {
            "contract": OVERLAP_CONTRACT,
            "digest": sha256_bytes(f"{OVERLAP_CONTRACT}:{change_id}:unavailable".encode()),
            "status": "unavailable",
            "exact_overlap_count": 0,
            "proximity_count": 0,
            "blocked": False,
            "next_action": None,
            "items": [],
        }
    registry = worktree_list(owner)
    valid = [entry for entry in registry["worktrees"] if entry.get("valid")]
    current = next((entry for entry in valid if entry.get("change_id") == change_id), None)
    if current is None:
        return {
            "contract": OVERLAP_CONTRACT,
            "digest": sha256_bytes(f"{OVERLAP_CONTRACT}:{change_id}:unavailable".encode()),
            "status": "unavailable",
            "exact_overlap_count": 0,
            "proximity_count": 0,
            "blocked": False,
            "next_action": None,
            "items": [],
        }
    current_paths, current_error = _changed_paths(current)
    items: list[dict[str, Any]] = []
    for other in valid:
        if other.get("change_id") == change_id:
            continue
        other_paths, other_error = _changed_paths(other)
        exact = sorted(current_paths & other_paths)
        current_roots = {Path(path).parts[0] for path in current_paths if Path(path).parts}
        other_roots = {Path(path).parts[0] for path in other_paths if Path(path).parts}
        proximity = sorted(current_roots & other_roots)
        if not exact and not proximity and current_error is None and other_error is None:
            continue
        predecessor, successor = sorted((current, other), key=_entry_key)
        if any(
            dependency.get("change_id") == other.get("change_id")
            for dependency in StateStore(owner).load(change_id).get("dependencies", [])
        ):
            predecessor, successor = other, current
        elif any(
            dependency.get("change_id") == change_id
            for dependency in StateStore(Path(other["owner_root"])).load(
                other["change_id"]
            ).get("dependencies", [])
        ):
            predecessor, successor = current, other
        blocks_current = bool(
            exact
            and successor.get("change_id") == change_id
            and not _accepted_in_base(owner, predecessor)
        )
        item: dict[str, Any] = {
            "change_id": other["change_id"],
            "status": "unavailable" if current_error or other_error else "detected",
            "reason": current_error or other_error,
            "exact_overlap_count": len(exact),
            "proximity_count": len(proximity),
            "predecessor_change_id": predecessor["change_id"],
            "successor_change_id": successor["change_id"],
            "blocks_current_candidate": blocks_current,
        }
        if verbose:
            item["exact_paths"] = exact[:32]
            item["exact_paths_omitted_count"] = max(0, len(exact) - 32)
            item["proximity_roots"] = proximity[:32]
        items.append(item)
    if current_error is not None and not items:
        return {
            "contract": OVERLAP_CONTRACT,
            "digest": sha256_bytes(f"{OVERLAP_CONTRACT}:{change_id}:unavailable".encode()),
            "status": "unavailable",
            "exact_overlap_count": 0,
            "proximity_count": 0,
            "blocked": False,
            "next_action": None,
            "items": [],
        }
    basis = [
        {
            key: value
            for key, value in item.items()
            if key not in {"exact_paths", "proximity_roots"}
        }
        for item in sorted(items, key=lambda value: value["change_id"])
    ]
    digest = sha256_bytes(
        json.dumps(
            {"contract": OVERLAP_CONTRACT, "change_id": change_id, "items": basis},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    blocked_items = [item for item in items if item["blocks_current_candidate"]]
    return {
        "contract": OVERLAP_CONTRACT,
        "digest": digest,
        "status": "available" if current_error is None else "unavailable",
        "exact_overlap_count": sum(item["exact_overlap_count"] for item in items),
        "proximity_count": sum(item["proximity_count"] for item in items),
        "blocked": bool(blocked_items),
        "next_action": (
            {
                "id": "wait-integration-predecessor",
                "detail": ",".join(
                    item["predecessor_change_id"] for item in blocked_items
                ),
            }
            if blocked_items
            else None
        ),
        "items": sorted(items, key=lambda value: value["change_id"]),
    }


def change_readiness(
    root: Path,
    *,
    change_id: str,
    stage: str,
    include_overlap: bool = False,
) -> dict[str, Any]:
    owner = resolve_change_root(root, change_id)
    state = StateStore(owner).load(change_id)
    dependencies = dependency_projection(owner, state, stage=stage)
    overlap = overlap_projection(owner, change_id=change_id)
    if not dependencies["satisfied"]:
        next_action = dependencies["next_action"]
    elif include_overlap and overlap["blocked"]:
        next_action = overlap["next_action"]
    else:
        next_action = None
    digest = sha256_bytes(
        json.dumps(
            {
                "contract": "dls-change-readiness/v1",
                "dependency_digest": dependencies["digest"],
                "overlap_digest": overlap["digest"],
                "stage": stage,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )
    return {
        "contract": "dls-change-readiness/v1",
        "digest": digest,
        "stage": stage,
        "ready": next_action is None,
        "dependencies": dependencies,
        "overlap": overlap,
        "next_action": next_action,
    }


def delivery_map(root: Path, *, verbose: bool = False) -> dict[str, Any]:
    registry = worktree_list(root)
    entries = [entry for entry in registry["worktrees"] if entry.get("valid")]
    by_change = {entry["change_id"]: entry for entry in entries}
    outgoing: dict[str, set[str]] = {change_id: set() for change_id in by_change}
    indegree: dict[str, int] = {change_id: 0 for change_id in by_change}
    for change_id, entry in by_change.items():
        state = StateStore(Path(entry["owner_root"])).load(change_id)
        for dependency in state.get("dependencies", []):
            target = dependency.get("change_id")
            if target in by_change and change_id not in outgoing[target]:
                outgoing[target].add(change_id)
                indegree[change_id] += 1
    queue = sorted(
        (by_change[change_id] for change_id, count in indegree.items() if count == 0),
        key=_entry_key,
    )
    integration_order: list[str] = []
    while queue:
        entry = queue.pop(0)
        change_id = entry["change_id"]
        integration_order.append(change_id)
        for dependent in sorted(outgoing[change_id]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(by_change[dependent])
                queue.sort(key=_entry_key)
    if len(integration_order) != len(by_change):
        raise IntegrityError("Registered delivery graph contains a dependency cycle")
    changes: list[dict[str, Any]] = []
    for entry in sorted(entries, key=lambda value: value["change_id"])[:DELIVERY_MAP_LIMIT]:
        owner = Path(entry["owner_root"])
        state = StateStore(owner).load(entry["change_id"])
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
            owner,
            change_id=entry["change_id"],
            stage=stage,
            include_overlap=stage in {"review", "acceptance"},
        )
        if readiness["next_action"] is not None:
            next_action = readiness["next_action"]
        elif stage == "definition":
            next_action = {"id": "continue-definition", "detail": "definition is ready"}
        elif stage == "implementation":
            next_action = {"id": "continue-implementation", "detail": "implementation is ready"}
        elif stage == "review":
            next_action = {"id": "continue-review", "detail": "review stage is ready"}
        else:
            next_action = {"id": "delivery-complete", "detail": "accepted"}
        item: dict[str, Any] = {
            "change_id": entry["change_id"],
            "branch": entry.get("branch"),
            "head_sha": git_head(owner),
            "phase": state.get("phase"),
            "lifecycle": state.get("lifecycle"),
            "definition_digest": current_definition_digest(owner, state),
            "dependency_digest": readiness["dependencies"]["digest"],
            "dependencies_satisfied": readiness["dependencies"]["satisfied"],
            "blocked_dependency_count": readiness["dependencies"]["blocked_count"],
            "exact_overlap_count": readiness["overlap"]["exact_overlap_count"],
            "proximity_count": readiness["overlap"]["proximity_count"],
            "parallel_ready": readiness["next_action"] is None,
            "next_action": next_action,
        }
        if verbose:
            item["owner_root"] = str(owner)
            item["dependencies"] = readiness["dependencies"]["items"]
            item["overlaps"] = readiness["overlap"]["items"]
        changes.append(item)
    ready_ids = [item["change_id"] for item in changes if item["parallel_ready"]]
    result = {
        "ok": all(entry.get("valid") for entry in registry["worktrees"]),
        "contract": DELIVERY_MAP_CONTRACT,
        "change_count": len(registry["worktrees"]),
        "omitted_count": max(0, len(entries) - DELIVERY_MAP_LIMIT),
        "parallel_groups": [ready_ids] if ready_ids else [],
        "integration_order": integration_order[:DELIVERY_MAP_LIMIT],
        "integration_order_omitted_count": max(
            0, len(integration_order) - DELIVERY_MAP_LIMIT
        ),
        "changes": changes,
    }
    if not verbose and len(json.dumps(result, ensure_ascii=False).encode()) > DELIVERY_MAP_MAX_BYTES:
        raise IntegrityError("delivery-map payload exceeds 16 KiB")
    return result
