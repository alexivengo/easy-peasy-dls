"""Explicit, repository-local routing for DLS change worktrees."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import IntegrityError, UsageError
from .io import FileLock, atomic_write_json, atomic_write_text, read_json, utc_now
from .repo import git_head, is_git_repo, run_git
from .state import (
    StateStore,
    current_definition_digest,
    definition_digest_at_revision,
    derived_approval_statuses,
    validate_change_id,
)

REGISTRY_SCHEMA_VERSION = 1
REGISTRY_DIRECTORY = "dls"
REGISTRY_FILENAME = "worktrees.json"
WORKTREE_PURPOSES = {"definition", "implementation"}


def _git_path(root: Path, argument: str) -> Path:
    value = run_git(root, "rev-parse", argument).stdout.strip()
    if not value:
        raise IntegrityError(f"Git did not return {argument} for {root}")
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def git_toplevel(root: Path) -> Path:
    if not is_git_repo(root):
        raise IntegrityError(f"Worktree routing requires a Git repository: {root}")
    return _git_path(root, "--show-toplevel")


def git_common_dir(root: Path) -> Path:
    if not is_git_repo(root):
        raise IntegrityError(f"Worktree routing requires a Git repository: {root}")
    return _git_path(root, "--git-common-dir")


def git_branch(root: Path) -> str | None:
    value = run_git(root, "branch", "--show-current").stdout.strip()
    return value or None


def _registered_git_worktrees(root: Path) -> set[Path]:
    output = run_git(root, "worktree", "list", "--porcelain", "-z").stdout
    return {
        Path(field.removeprefix("worktree ")).resolve()
        for field in output.split("\0")
        if field.startswith("worktree ")
    }


def resolve_change_root(root: Path, change_id: str) -> Path:
    """Resolve one change without scanning sibling directories or branches."""
    candidate = root.resolve()
    if not is_git_repo(candidate):
        if (
            (candidate / ".dls" / "config.toml").is_file()
            and StateStore(candidate).path(change_id).is_file()
        ):
            return candidate
        return git_toplevel(candidate)
    candidate = git_toplevel(candidate)
    _, registry = _load_registry(candidate, required=False)
    registered = registry["worktrees"].get(change_id)
    if registered is not None:
        if not isinstance(registered, dict):
            raise IntegrityError(
                f"Registered owner for {change_id} is not a metadata object"
            )
        return Path(
            _validated_entry(
                candidate,
                change_id=change_id,
                entry=registered,
            )["owner_root"]
        )
    if (
        (candidate / ".dls" / "config.toml").is_file()
        and StateStore(candidate).path(change_id).is_file()
    ):
        return candidate
    return resolve_registered_worktree(candidate, change_id)


def worktree_registry_path(root: Path) -> Path:
    return git_common_dir(root) / REGISTRY_DIRECTORY / REGISTRY_FILENAME


def owner_preparation_required(
    root: Path,
    *,
    change_id: str,
    state: dict[str, Any],
) -> bool:
    """Return whether parallel standard/critical work still lacks an owner."""
    if state.get("control_level") not in {"standard", "critical"}:
        return False
    caller = git_toplevel(root)
    _, registry = _load_registry(caller, required=False)
    registered = registry["worktrees"].get(change_id)
    if isinstance(registered, dict):
        return Path(str(registered.get("owner_root", ""))).resolve() != caller
    return any(
        other_change != change_id and isinstance(entry, dict)
        for other_change, entry in registry["worktrees"].items()
    )


def _empty_registry(common_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "git_common_dir": str(common_dir),
        "worktrees": {},
    }


def _load_registry(root: Path, *, required: bool) -> tuple[Path, dict[str, Any]]:
    common_dir = git_common_dir(root)
    path = common_dir / REGISTRY_DIRECTORY / REGISTRY_FILENAME
    if not path.is_file():
        if required:
            raise IntegrityError(
                "No DLS worktree registry for this Git repository. DLS will not infer "
                "branches or scan neighboring worktrees; run "
                f"`dls worktree register CHANGE_ID /absolute/worktree/path` from {root}"
            )
        return path, _empty_registry(common_dir)
    registry = read_json(path)
    if registry.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        raise IntegrityError("Unsupported DLS worktree registry schema")
    if registry.get("git_common_dir") != str(common_dir):
        raise IntegrityError("DLS worktree registry belongs to another Git common-dir")
    entries = registry.get("worktrees")
    if not isinstance(entries, dict):
        raise IntegrityError("DLS worktree registry worktrees must be an object")
    return path, registry


def _validated_entry(
    root: Path,
    *,
    change_id: str,
    entry: dict[str, Any],
) -> dict[str, Any]:
    validate_change_id(change_id)
    owner_value = entry.get("owner_root")
    if not isinstance(owner_value, str) or not Path(owner_value).is_absolute():
        raise IntegrityError(f"Registered worktree has an invalid owner_root: {change_id}")
    owner = Path(owner_value).resolve()
    if not owner.is_dir() or git_toplevel(owner) != owner:
        raise IntegrityError(f"Registered worktree is missing or invalid: {owner}")
    caller_common_dir = git_common_dir(root)
    owner_common_dir = git_common_dir(owner)
    if owner_common_dir != caller_common_dir:
        raise IntegrityError(
            f"Registered worktree belongs to another Git repository: {owner}"
        )
    if entry.get("git_common_dir") != str(caller_common_dir):
        raise IntegrityError(
            f"Registered worktree common-dir metadata is stale: {change_id}"
        )
    if owner not in _registered_git_worktrees(root):
        raise IntegrityError(f"Path is no longer a registered Git worktree: {owner}")
    registered_branch = entry.get("branch")
    if registered_branch is not None and not isinstance(registered_branch, str):
        raise IntegrityError(f"Registered worktree has invalid branch metadata: {change_id}")
    current_branch = git_branch(owner)
    if current_branch != registered_branch:
        raise IntegrityError(
            f"Registered worktree branch changed: expected "
            f"{registered_branch or '<detached>'}, got {current_branch or '<detached>'}"
        )
    if not (owner / ".dls" / "config.toml").is_file():
        raise IntegrityError(f"Registered worktree is not initialized for DLS: {owner}")
    state = StateStore(owner).load(change_id)
    base_sha = entry.get("base_sha")
    if base_sha is not None:
        if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", base_sha):
            raise IntegrityError(f"Registered worktree has invalid base_sha: {change_id}")
        resolved_base = run_git(owner, "rev-parse", "--verify", f"{base_sha}^{{commit}}", check=False)
        if resolved_base.returncode != 0 or resolved_base.stdout.strip() != base_sha:
            raise IntegrityError(f"Registered worktree base commit is missing: {change_id}")
    purpose = entry.get("purpose")
    if purpose is not None and purpose not in WORKTREE_PURPOSES:
        raise IntegrityError(f"Registered worktree has invalid purpose: {change_id}")
    return {
        **entry,
        "change_id": change_id,
        "owner_root": str(owner),
        "branch": current_branch,
        "state_revision": state["state_revision"],
        "valid": True,
    }


def resolve_registered_worktree(root: Path, change_id: str) -> Path:
    _, registry = _load_registry(root, required=True)
    entry = registry["worktrees"].get(validate_change_id(change_id))
    if not isinstance(entry, dict):
        raise IntegrityError(
            f"No registered worktree for {change_id}. DLS will not infer branches or "
            "scan neighboring worktrees; run "
            "`dls worktree register "
            f"{change_id} /absolute/worktree/path`"
        )
    validated = _validated_entry(root, change_id=change_id, entry=entry)
    return Path(validated["owner_root"])


def worktree_register(
    root: Path,
    *,
    change_id: str,
    owner_path: Path,
    base_ref: str | None = None,
    purpose: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    change_id = validate_change_id(change_id)
    if not owner_path.is_absolute():
        raise UsageError("Worktree registration requires an absolute path")
    caller_root = git_toplevel(root)
    owner = owner_path.resolve()
    if not owner.is_dir() or git_toplevel(owner) != owner:
        raise IntegrityError(f"Worktree owner must be an existing Git toplevel: {owner}")
    common_dir = git_common_dir(caller_root)
    if git_common_dir(owner) != common_dir:
        raise IntegrityError("Cannot register a worktree from another Git repository")
    if owner not in _registered_git_worktrees(caller_root):
        raise IntegrityError(f"Path is not registered by Git as a worktree: {owner}")
    if not (owner / ".dls" / "config.toml").is_file():
        raise IntegrityError(f"Worktree is not initialized for DLS: {owner}")
    StateStore(owner).load(change_id)
    if purpose is not None and purpose not in WORKTREE_PURPOSES:
        raise UsageError(f"Unsupported worktree purpose: {purpose}")
    base_sha = None
    if base_ref is not None:
        base_sha = run_git(
            owner,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{base_ref}^{{commit}}",
        ).stdout.strip()
    path, registry = _load_registry(caller_root, required=False)
    entry = {
        "owner_root": str(owner),
        "git_common_dir": str(common_dir),
        "branch": git_branch(owner),
        "registered_at": utc_now(),
    }
    if base_sha is not None:
        entry["base_sha"] = base_sha
    if purpose is not None:
        entry["purpose"] = purpose
    existing = registry["worktrees"].get(change_id)
    if isinstance(existing, dict):
        if base_ref is None and isinstance(existing.get("base_sha"), str):
            entry["base_sha"] = existing["base_sha"]
        if purpose is None and isinstance(existing.get("purpose"), str):
            entry["purpose"] = existing["purpose"]
        stable_existing = {
            key: existing.get(key)
            for key in ("owner_root", "git_common_dir", "branch", "base_sha", "purpose")
        }
        stable_entry = {
            key: entry.get(key)
            for key in ("owner_root", "git_common_dir", "branch", "base_sha", "purpose")
        }
        if stable_existing == stable_entry:
            return {
                "ok": True,
                "dry_run": dry_run,
                "changed": False,
                "change_id": change_id,
                "registry_path": str(path),
                "worktree": _validated_entry(
                    caller_root,
                    change_id=change_id,
                    entry=existing,
                ),
            }
        raise IntegrityError(
            f"{change_id} is already registered to {existing.get('owner_root')}; "
            "unregister it before changing owners"
        )
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "registry_path": str(path),
            "worktree": {**entry, "change_id": change_id, "valid": True},
        }
    lock_path = path.with_suffix(path.suffix + ".lock")
    with FileLock(lock_path):
        _, current = _load_registry(caller_root, required=False)
        if change_id in current["worktrees"]:
            raise IntegrityError(
                f"{change_id} was registered concurrently; retry after `dls worktree list`"
            )
        current["worktrees"][change_id] = entry
        atomic_write_json(path, current, backup=False)
    return {
        "ok": True,
        "dry_run": False,
        "changed": True,
        "change_id": change_id,
        "registry_path": str(path),
        "worktree": _validated_entry(
            caller_root,
            change_id=change_id,
            entry=entry,
        ),
    }


def _default_worktree_values(root: Path, change_id: str, purpose: str) -> tuple[Path, str]:
    slug = re.sub(r"[^a-z0-9]+", "-", change_id.lower()).strip("-")
    if not slug:
        raise UsageError("Change ID cannot produce a safe worktree branch")
    return root.parent / f"{root.name}-{change_id}-{purpose}", f"codex/{slug}-{purpose}"


def worktree_create(
    root: Path,
    *,
    change_id: str,
    base_ref: str,
    purpose: str,
    owner_path: Path | None = None,
    branch: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create one isolated Git worktree from an explicitly resolved commit."""
    change_id = validate_change_id(change_id)
    if purpose not in WORKTREE_PURPOSES:
        raise UsageError(f"Unsupported worktree purpose: {purpose}")
    caller_root = git_toplevel(root)
    base_sha = run_git(
        caller_root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{base_ref}^{{commit}}",
    ).stdout.strip()
    default_path, default_branch = _default_worktree_values(
        caller_root, change_id, purpose
    )
    target = (owner_path or default_path)
    if not target.is_absolute():
        raise UsageError("Worktree creation requires an absolute --path")
    target = target.resolve()
    branch_name = branch or default_branch
    if (
        not re.fullmatch(r"codex/[A-Za-z0-9._/-]{1,120}", branch_name)
        or run_git(
            caller_root,
            "check-ref-format",
            "--branch",
            branch_name,
            check=False,
        ).returncode
        != 0
    ):
        raise UsageError("Worktree branch must be a safe codex/* branch")
    registered = _registered_git_worktrees(caller_root)
    if target in registered:
        if git_common_dir(target) != git_common_dir(caller_root):
            raise IntegrityError("Existing worktree belongs to another Git repository")
        if git_branch(target) != branch_name:
            raise IntegrityError("Existing worktree branch does not match requested branch")
        existing_head = run_git(target, "rev-parse", "HEAD").stdout.strip()
        return {
            "ok": True,
            "dry_run": dry_run,
            "changed": False,
            "change_id": change_id,
            "purpose": purpose,
            "base_sha": base_sha,
            "branch": branch_name,
            "owner_root": str(target),
            "head_sha": existing_head,
            "next_action": {
                "id": "prepare-owner-worktree",
                "detail": "complete DLS state handoff and owner registration",
            },
        }
    if target.exists():
        raise IntegrityError(f"Worktree path already exists: {target}")
    branch_ref = f"refs/heads/{branch_name}"
    if run_git(caller_root, "show-ref", "--verify", branch_ref, check=False).returncode == 0:
        raise IntegrityError(f"Worktree branch already exists: {branch_name}")
    projected = {
        "ok": True,
        "dry_run": dry_run,
        "changed": not dry_run,
        "change_id": change_id,
        "purpose": purpose,
        "base_sha": base_sha,
        "branch": branch_name,
        "owner_root": str(target),
        "head_sha": base_sha,
        "next_action": {
            "id": "prepare-owner-worktree",
            "detail": "complete DLS state handoff and owner registration",
        },
    }
    if dry_run:
        projected["changed"] = False
        return projected
    run_git(
        caller_root,
        "worktree",
        "add",
        "-b",
        branch_name,
        str(target),
        base_sha,
    )
    return projected


def _copy_if_identical_or_missing(source: Path, target: Path) -> list[tuple[Path, str]]:
    payload = source.read_text(encoding="utf-8")
    if target.exists():
        if not target.is_file() or target.read_text(encoding="utf-8") != payload:
            raise IntegrityError(f"Prepared worktree configuration differs: {target}")
        return []
    return [(target, payload)]


def _copy_tree_immutable(source: Path, target: Path) -> list[tuple[Path, str]]:
    if not source.exists():
        return []
    planned: list[tuple[Path, str]] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise IntegrityError(f"DLS handoff refuses symlinked metadata: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        destination = target / relative
        payload = path.read_text(encoding="utf-8")
        if destination.exists() and destination.read_text(encoding="utf-8") != payload:
            raise IntegrityError(
                f"DLS handoff immutable artifact differs: {destination}"
            )
        if not destination.exists():
            planned.append((destination, payload))
    return planned


def _rollback_transfer(journal: dict[str, Any]) -> None:
    for path, previous in reversed(journal["files"]):
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, previous, backup=False)
    if journal.get("cache_created"):
        try:
            journal["cache_path"].rmdir()
        except OSError:
            pass


def _transfer_change_metadata(
    source: Path,
    target: Path,
    *,
    change_id: str,
    allow_committed_snapshot_replacement: bool,
) -> dict[str, Any]:
    source_dls = source / ".dls"
    target_dls = target / ".dls"
    planned = _copy_if_identical_or_missing(
        source_dls / "config.toml", target_dls / "config.toml"
    )
    source_ignore = source_dls / ".gitignore"
    if source_ignore.is_file():
        planned.extend(
            _copy_if_identical_or_missing(source_ignore, target_dls / ".gitignore")
        )
    source_profiles = source_dls / "profiles"
    if source_profiles.is_dir():
        planned.extend(_copy_tree_immutable(source_profiles, target_dls / "profiles"))

    source_state = StateStore(source).load(change_id)
    target_state_path = StateStore(target).path(change_id)
    if target_state_path.is_file():
        existing = read_json(target_state_path)
        if (
            existing.get("change_id") != change_id
            or existing.get("artifacts") != source_state.get("artifacts")
            or existing.get("work_kind") != source_state.get("work_kind")
            or existing.get("control_level") != source_state.get("control_level")
        ):
            raise IntegrityError(
                "Prepared worktree contains a different DLS change state"
            )
        if existing != source_state and not allow_committed_snapshot_replacement:
            relative = target_state_path.relative_to(target).as_posix()
            committed = run_git(
                target,
                "show",
                f"HEAD:{relative}",
                check=False,
            )
            try:
                committed_state = (
                    json.loads(committed.stdout)
                    if committed.returncode == 0
                    else None
                )
            except json.JSONDecodeError:
                committed_state = None
            if committed_state != existing:
                raise IntegrityError(
                    "Prepared worktree contains newer local DLS state; refusing to overwrite it"
                )
    for category in ("evidence", "reviews"):
        planned.extend(
            _copy_tree_immutable(
                source_dls / category / change_id,
                target_dls / category / change_id,
            )
        )
    unique_destinations = {path for path, _ in planned}
    unique_destinations.add(target_state_path)
    journal = {
        "files": [
            (
                path,
                path.read_text(encoding="utf-8") if path.is_file() else None,
            )
            for path in sorted(unique_destinations)
        ],
        "cache_path": target_dls / "cache",
        "cache_created": not (target_dls / "cache").exists(),
    }
    try:
        for destination, payload in planned:
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(destination, payload, backup=False)
        atomic_write_json(target_state_path, source_state, backup=False)
        journal["cache_path"].mkdir(parents=True, exist_ok=True)
    except Exception:
        _rollback_transfer(journal)
        raise
    return journal


def _register_prepared_owner(
    caller_root: Path,
    *,
    source_root: Path,
    target: Path,
    change_id: str,
    base_sha: str,
    purpose: str,
) -> tuple[dict[str, Any], bool]:
    path, _ = _load_registry(caller_root, required=False)
    entry = {
        "owner_root": str(target),
        "git_common_dir": str(git_common_dir(caller_root)),
        "branch": git_branch(target),
        "base_sha": base_sha,
        "purpose": purpose,
        "registered_at": utc_now(),
    }
    validated_entry = _validated_entry(
        caller_root,
        change_id=change_id,
        entry=entry,
    )
    lock_path = path.with_suffix(path.suffix + ".lock")
    with FileLock(lock_path):
        _, current = _load_registry(caller_root, required=False)
        existing = current["worktrees"].get(change_id)
        if isinstance(existing, dict):
            existing_owner = Path(str(existing.get("owner_root", ""))).resolve()
            if existing_owner == target:
                if any(
                    existing.get(key) != entry.get(key)
                    for key in ("git_common_dir", "branch", "base_sha", "purpose")
                ):
                    raise IntegrityError(
                        f"{change_id} owner registration metadata differs from the prepared handoff"
                    )
                return _validated_entry(
                    caller_root,
                    change_id=change_id,
                    entry=existing,
                ), False
            if existing_owner != source_root:
                raise IntegrityError(
                    f"{change_id} already has another canonical owner: {existing_owner}"
                )
        current["worktrees"][change_id] = entry
        atomic_write_json(path, current, backup=False)
    return validated_entry, True


def worktree_prepare(
    root: Path,
    *,
    change_id: str,
    base_ref: str,
    purpose: str,
    owner_path: Path | None = None,
    branch: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create and register one owner while preserving approved DLS state.

    This is the implementation handoff primitive.  It refuses to reconstruct
    state through ``adopt`` and rolls back a newly-created worktree if metadata
    transfer or registration cannot be proven complete.
    """
    if purpose != "implementation":
        raise UsageError("Atomic worktree preparation currently owns implementation handoff")
    change_id = validate_change_id(change_id)
    caller_root = git_toplevel(root)
    source_root = resolve_change_root(caller_root, change_id)
    source_state = StateStore(source_root).load(change_id)
    if source_state.get("control_level") not in {"standard", "critical"}:
        raise UsageError("Atomic owner preparation is only for standard or critical work")
    approval = next(
        (
            item
            for item in reversed(derived_approval_statuses(source_root, source_state))
            if item.get("decision") == "definition" and item.get("status") == "current"
        ),
        None,
    )
    if approval is None:
        raise IntegrityError("Owner worktree preparation requires current definition approval")
    base_sha = run_git(
        caller_root,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{base_ref}^{{commit}}",
    ).stdout.strip()
    expected_digest = current_definition_digest(source_root, source_state)
    base_digest = definition_digest_at_revision(source_root, source_state, base_sha)
    if base_digest != expected_digest:
        raise IntegrityError(
            "Owner worktree base does not reproduce the approved definition"
        )
    preview = worktree_create(
        caller_root,
        change_id=change_id,
        base_ref=base_sha,
        purpose=purpose,
        owner_path=owner_path,
        branch=branch,
        dry_run=True,
    )
    if dry_run:
        return {
            **preview,
            "status": "projected",
            "definition_digest": expected_digest,
            "approval_id": approval.get("id"),
            "next_action": {
                "id": "continue-implementation",
                "detail": "approved owner handoff can be completed",
            },
        }

    created = worktree_create(
        caller_root,
        change_id=change_id,
        base_ref=base_sha,
        purpose=purpose,
        owner_path=owner_path,
        branch=branch,
        dry_run=False,
    )
    target = Path(created["owner_root"]).resolve()
    created_here = bool(created["changed"])
    transfer_journal: dict[str, Any] | None = None
    try:
        transfer_journal = _transfer_change_metadata(
            source_root,
            target,
            change_id=change_id,
            allow_committed_snapshot_replacement=created_here,
        )
        transferred_state = StateStore(target).load(change_id)
        if current_definition_digest(target, transferred_state) != expected_digest:
            raise IntegrityError("Transferred owner changed the approved definition digest")
        transferred_approval = next(
            (
                item
                for item in reversed(derived_approval_statuses(target, transferred_state))
                if item.get("decision") == "definition"
                and item.get("status") == "current"
            ),
            None,
        )
        if transferred_approval is None:
            raise IntegrityError("Transferred owner lost definition approval")
        validated, registration_changed = _register_prepared_owner(
            caller_root,
            source_root=source_root,
            target=target,
            change_id=change_id,
            base_sha=base_sha,
            purpose=purpose,
        )
    except Exception:
        if created_here:
            run_git(caller_root, "worktree", "remove", "--force", str(target), check=False)
            run_git(
                caller_root,
                "branch",
                "-D",
                str(created["branch"]),
                check=False,
            )
        elif transfer_journal is not None:
            _rollback_transfer(transfer_journal)
        raise
    return {
        "ok": True,
        "dry_run": False,
        "changed": created_here or registration_changed,
        "change_id": change_id,
        "purpose": purpose,
        "base_sha": base_sha,
        "branch": created["branch"],
        "owner_root": str(target),
        "head_sha": git_head(target),
        "definition_digest": expected_digest,
        "approval_id": transferred_approval.get("id"),
        "worktree": validated,
        "next_action": {
            "id": "continue-implementation",
            "detail": "canonical owner is ready",
        },
    }


def worktree_list(root: Path) -> dict[str, Any]:
    path, registry = _load_registry(root, required=False)
    entries: list[dict[str, Any]] = []
    for change_id, entry in sorted(registry["worktrees"].items()):
        if not isinstance(entry, dict):
            entries.append(
                {
                    "change_id": change_id,
                    "valid": False,
                    "error": "registry entry is not an object",
                }
            )
            continue
        try:
            entries.append(_validated_entry(root, change_id=change_id, entry=entry))
        except (IntegrityError, UsageError) as exc:
            entries.append(
                {
                    **entry,
                    "change_id": change_id,
                    "valid": False,
                    "error": str(exc),
                }
            )
    return {
        "ok": all(entry["valid"] for entry in entries),
        "registry_path": str(path),
        "worktrees": entries,
    }


def worktree_verify(root: Path, *, change_id: str) -> dict[str, Any]:
    _, registry = _load_registry(root, required=True)
    change_id = validate_change_id(change_id)
    entry = registry["worktrees"].get(change_id)
    if not isinstance(entry, dict):
        raise IntegrityError(f"No registered worktree for {change_id}")
    return {
        "ok": True,
        "change_id": change_id,
        "registry_path": str(worktree_registry_path(root)),
        "worktree": _validated_entry(root, change_id=change_id, entry=entry),
    }


def worktree_unregister(
    root: Path,
    *,
    change_id: str,
    dry_run: bool = False,
) -> dict[str, Any]:
    path, registry = _load_registry(root, required=False)
    change_id = validate_change_id(change_id)
    existing = registry["worktrees"].get(change_id)
    if existing is None:
        return {
            "ok": True,
            "dry_run": dry_run,
            "changed": False,
            "change_id": change_id,
            "registry_path": str(path),
        }
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "changed": False,
            "change_id": change_id,
            "registry_path": str(path),
        }
    lock_path = path.with_suffix(path.suffix + ".lock")
    with FileLock(lock_path):
        _, current = _load_registry(root, required=True)
        removed = current["worktrees"].pop(change_id, None)
        if removed is None:
            return {
                "ok": True,
                "dry_run": False,
                "changed": False,
                "change_id": change_id,
                "registry_path": str(path),
            }
        atomic_write_json(path, current, backup=False)
    return {
        "ok": True,
        "dry_run": False,
        "changed": True,
        "change_id": change_id,
        "registry_path": str(path),
    }
