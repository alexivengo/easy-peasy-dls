"""Minimal change-to-worktree ownership backed by Git's own worktree list."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .core import load_state, validate_change_id
from .errors import IntegrityError, UsageError
from .io import FileLock, atomic_write_json, read_json, safe_resolve
from .repo import git_head, git_source_dirty_paths, is_git_repo, run_git

REGISTRY_SCHEMA = 2


def _git_path(root: Path, name: str) -> Path:
    value = run_git(root, "rev-parse", name).stdout.strip()
    path = Path(value)
    return (path if path.is_absolute() else root / path).resolve()


def git_toplevel(root: Path) -> Path:
    if not is_git_repo(root):
        raise IntegrityError(f"Worktree routing requires Git: {root}")
    return _git_path(root, "--show-toplevel")


def git_common_dir(root: Path) -> Path:
    if not is_git_repo(root):
        raise IntegrityError(f"Worktree routing requires Git: {root}")
    return _git_path(root, "--git-common-dir")


def registry_path(root: Path) -> Path:
    return git_common_dir(root) / "dls" / "owners.json"


def _worktrees(root: Path) -> dict[str, Path]:
    result = run_git(root, "worktree", "list", "--porcelain", "-z")
    output: dict[str, Path] = {}
    for token in result.stdout.split("\0"):
        if not token.startswith("worktree "):
            continue
        path = Path(token.removeprefix("worktree ")).resolve()
        if not path.is_dir():
            continue
        gitdir = _git_path(path, "--absolute-git-dir")
        common = git_common_dir(root)
        try:
            identity = gitdir.relative_to(common).as_posix()
        except ValueError as exc:
            raise IntegrityError("Git worktree belongs to another common-dir") from exc
        output[identity] = path
    return output


def _state_worktrees(root: Path, change_id: str) -> list[Path]:
    """Return Git-known worktrees carrying state for one change.

    Git remains the discovery boundary: this never scans sibling directories or
    guesses from branch names.
    """

    caller = git_toplevel(root)
    return sorted(
        (
            path
            for path in _worktrees(caller).values()
            if (path / ".dls" / "state" / f"{change_id}.json").is_file()
        ),
        key=lambda item: str(item),
    )


def _load_registry(root: Path) -> dict[str, Any]:
    path = registry_path(root)
    if not path.exists():
        legacy = git_common_dir(root) / "dls" / "worktrees.json"
        if legacy.is_file():
            value = read_json(legacy)
            owners: dict[str, str] = {}
            common = git_common_dir(root)
            for change_id, item in value.get("worktrees", {}).items():
                owner_value = item.get("owner_root") if isinstance(item, dict) else None
                if not isinstance(owner_value, str):
                    continue
                owner = Path(owner_value)
                if not owner.is_dir():
                    continue
                gitdir = _git_path(owner, "--absolute-git-dir")
                if gitdir.is_relative_to(common):
                    owners[change_id] = gitdir.relative_to(common).as_posix()
            return {"schema_version": REGISTRY_SCHEMA, "owners": owners}
        return {"schema_version": REGISTRY_SCHEMA, "owners": {}}
    value = read_json(path)
    if value.get("schema_version") == 1:
        owners: dict[str, str] = {}
        for change_id, item in value.get("worktrees", {}).items():
            if not isinstance(item, dict):
                continue
            owner = item.get("owner_root")
            if not isinstance(owner, str):
                continue
            candidate = Path(owner)
            if candidate.is_dir():
                common = git_common_dir(root)
                gitdir = _git_path(candidate, "--absolute-git-dir")
                if gitdir.is_relative_to(common):
                    owners[change_id] = gitdir.relative_to(common).as_posix()
        return {"schema_version": REGISTRY_SCHEMA, "owners": owners}
    if value.get("schema_version") != REGISTRY_SCHEMA or not isinstance(value.get("owners"), dict):
        raise IntegrityError("Unsupported DLS worktree owner registry")
    return value


def _save_registry(root: Path, value: dict[str, Any]) -> None:
    atomic_write_json(registry_path(root), value, backup=False)


def resolve_change_root(root: Path, change_id: str) -> Path:
    owner, context = execution_context(root, change_id)
    if owner is None:
        raise IntegrityError(context["detail"])
    return owner


def execution_context(root: Path, change_id: str) -> tuple[Path | None, dict[str, Any]]:
    """Resolve the local execution root without mutating product or registry state."""

    validate_change_id(change_id)
    caller = git_toplevel(root)
    registry = _load_registry(caller)
    live = _worktrees(caller)
    identity = registry["owners"].get(change_id)
    owner: Path | None = None
    status = "ready"
    action = "continue-in-owner"
    detail = "Use the resolved owner worktree."
    discovered = False

    if identity is not None:
        if not isinstance(identity, str) or Path(identity).is_absolute() or ".." in Path(identity).parts:
            return None, {
                "contract": "dls-execution-context/v1",
                "status": "conflict",
                "action": "resolve-owner-conflict",
                "reason": "invalid-owner-identity",
                "detail": f"Invalid worktree identity for {change_id}",
                "caller_root": str(caller),
                "owner_root": None,
            }
        owner = live.get(identity)
        if owner is None:
            return None, {
                "contract": "dls-execution-context/v1",
                "status": "conflict",
                "action": "resolve-owner-conflict",
                "reason": "owner-unavailable",
                "detail": f"Owner worktree is unavailable for {change_id}",
                "caller_root": str(caller),
                "owner_root": None,
            }
    else:
        candidates = _state_worktrees(caller, change_id)
        non_callers = [item for item in candidates if item != caller]
        if len(non_callers) == 1:
            owner = non_callers[0]
            discovered = True
        elif len(non_callers) > 1:
            return None, {
                "contract": "dls-execution-context/v1",
                "status": "conflict",
                "action": "resolve-owner-conflict",
                "reason": "ambiguous-owner",
                "detail": f"Multiple Git worktrees contain state for {change_id}",
                "caller_root": str(caller),
                "owner_root": None,
            }
        elif (caller / ".dls" / "state" / f"{change_id}.json").is_file():
            owner = caller
        else:
            return None, {
                "contract": "dls-execution-context/v1",
                "status": "missing",
                "action": "prepare-owner-worktree",
                "reason": "state-unavailable",
                "detail": f"No DLS state is available for {change_id}",
                "caller_root": str(caller),
                "owner_root": None,
            }

    assert owner is not None
    state = load_state(owner, change_id, allow_legacy=True)
    control = state.get("change", {}).get("control")
    phase = state.get("phase")
    requires_owner = control in {"standard", "critical"} and phase in {
        "implementation",
        "review",
        "accepted",
    }
    if owner == caller and identity is None and requires_owner:
        status = "prepare"
        action = "prepare-owner-worktree"
        detail = "Prepare an isolated owner worktree before product changes."
    elif discovered:
        status = "discovered"
        action = "bind-owner-worktree"
        detail = "Bind the single Git-known worktree carrying this change."

    owner_dirty = bool(git_source_dirty_paths(owner))
    if owner_dirty and owner != caller:
        status = "conflict"
        action = "commit-owner-source"
        detail = "The owner worktree contains uncommitted product changes."

    return owner, {
        "contract": "dls-execution-context/v1",
        "status": status,
        "action": action,
        "reason": None,
        "detail": detail,
        "caller_root": str(caller),
        "owner_root": str(owner),
        "caller_is_owner": caller == owner,
        "caller_dirty": bool(git_source_dirty_paths(caller)),
        "owner_dirty": owner_dirty,
        "owner_head": git_head(owner),
        "registry_bound": identity is not None,
    }


def bind_owner(root: Path, change_id: str, owner: Path) -> None:
    caller = git_toplevel(root)
    owner = git_toplevel(owner)
    if git_common_dir(caller) != git_common_dir(owner):
        raise IntegrityError("Owner worktree belongs to another repository")
    common = git_common_dir(caller)
    gitdir = _git_path(owner, "--absolute-git-dir")
    identity = gitdir.relative_to(common).as_posix()
    path = registry_path(caller)
    with FileLock(path.with_suffix(".lock")):
        registry = _load_registry(caller)
        existing = registry["owners"].get(change_id)
        if existing is not None and existing != identity:
            raise IntegrityError(f"{change_id} already has another owner worktree")
        registry["owners"][change_id] = identity
        _save_registry(caller, registry)


def migrate_registry(root: Path, *, apply: bool) -> dict[str, Any]:
    registry = _load_registry(root)
    if apply:
        _save_registry(root, registry)
    return {"owners": len(registry["owners"]), "written": apply}


def _copy_metadata(
    source: Path,
    target: Path,
    change_id: str,
    *,
    replace_checkout_snapshot: bool = False,
) -> None:
    (target / ".dls" / "state").mkdir(parents=True, exist_ok=True)
    source_state = source / ".dls" / "state" / f"{change_id}.json"
    if not source_state.is_file():
        raise IntegrityError(f"Missing DLS state for {change_id}")
    target_state = target / ".dls" / "state" / source_state.name
    if (
        target_state.is_file()
        and target_state.read_bytes() != source_state.read_bytes()
        and not replace_checkout_snapshot
    ):
        raise IntegrityError(f"Existing owner state differs for {change_id}")
    source_config = source / ".dls" / "config.toml"
    target_config = target / ".dls" / "config.toml"
    if (
        target_config.is_file()
        and target_config.read_bytes() != source_config.read_bytes()
        and not replace_checkout_snapshot
    ):
        raise IntegrityError("Existing owner config differs")
    if not target_config.exists() or replace_checkout_snapshot:
        shutil.copy2(source_config, target_config)
    if not target_state.exists() or replace_checkout_snapshot:
        shutil.copy2(source_state, target_state)
    state = read_json(source_state)
    references = {
        item.get(key)
        for item, key in (
            (state.get("candidate") or {}, "pack_path"),
            (state.get("review") or {}, "result_path"),
            (state.get("definition_review") or {}, "result_path"),
        )
        if isinstance(item, dict) and isinstance(item.get(key), str)
    }
    for relative in sorted(references):
        source_artifact = safe_resolve(source, relative, must_exist=True)
        target_artifact = safe_resolve(target, relative)
        if (
            target_artifact.is_file()
            and target_artifact.read_bytes() != source_artifact.read_bytes()
            and not replace_checkout_snapshot
        ):
            raise IntegrityError(f"Existing owner artifact differs: {relative}")
        if not target_artifact.exists() or replace_checkout_snapshot:
            target_artifact.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_artifact, target_artifact)
    profile_dir = source / ".dls" / "profiles"
    if profile_dir.is_dir():
        target_profiles = target / ".dls" / "profiles"
        for profile in profile_dir.glob("*.toml"):
            target_profile = target_profiles / profile.name
            if (
                target_profile.is_file()
                and target_profile.read_bytes() != profile.read_bytes()
                and not replace_checkout_snapshot
            ):
                raise IntegrityError(f"Existing owner profile differs: {profile.name}")
            target_profiles.mkdir(parents=True, exist_ok=True)
            if not target_profile.exists() or replace_checkout_snapshot:
                shutil.copy2(profile, target_profile)


def prepare(
    root: Path,
    *,
    change_id: str,
    base: str | None,
    path: Path | None,
    branch: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    caller = git_toplevel(root)
    validate_change_id(change_id)
    requested_base = base or "HEAD"
    resolved = run_git(caller, "rev-parse", "--verify", f"{requested_base}^{{commit}}", check=False)
    base_sha = resolved.stdout.strip()
    if resolved.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", base_sha):
        raise UsageError(f"Unknown worktree base: {requested_base}")
    default_path = caller.parent / f"{caller.name}-{change_id}-implementation"
    target = (path or default_path).resolve()
    target_branch = branch or f"codex/{change_id}-implementation"
    registry = _load_registry(caller)
    identity = registry["owners"].get(change_id)
    if identity is not None:
        owner = _worktrees(caller).get(identity)
        if owner is None:
            raise IntegrityError(f"Owner worktree is unavailable for {change_id}")
        return {
            "ok": True,
            "dry_run": dry_run,
            "created": False,
            "change_id": change_id,
            "head_sha": git_head(owner),
            "owner": str(owner),
        }
    discovered = [item for item in _state_worktrees(caller, change_id) if item != caller]
    if len(discovered) > 1:
        raise IntegrityError(f"Multiple Git worktrees contain state for {change_id}")
    if len(discovered) == 1:
        owner = discovered[0]
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "created": False,
                "change_id": change_id,
                "head_sha": git_head(owner),
                "owner": str(owner),
                "binding_recovered": True,
            }
        bind_owner(caller, change_id, owner)
        return {
            "ok": True,
            "dry_run": False,
            "created": False,
            "change_id": change_id,
            "head_sha": git_head(owner),
            "owner": str(owner),
            "binding_recovered": True,
        }
    if target.exists():
        registered = set(_worktrees(caller).values())
        if target not in registered:
            raise IntegrityError(f"Worktree path already exists: {target}")
        state_path = target / ".dls" / "state" / f"{change_id}.json"
        if state_path.is_file():
            load_state(target, change_id, allow_legacy=True)
        if dry_run:
            return {"ok": True, "dry_run": True, "created": False, "owner": str(target)}
        _copy_metadata(caller, target, change_id)
        bind_owner(caller, change_id, target)
        return {"ok": True, "dry_run": False, "created": False, "owner": str(target)}
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "created": True,
            "change_id": change_id,
            "base_sha": base_sha,
            "branch": target_branch,
            "owner": str(target),
        }
    branch_exists = run_git(
        caller,
        "show-ref",
        "--verify",
        f"refs/heads/{target_branch}",
        check=False,
    ).returncode == 0
    if branch_exists:
        branch_head = run_git(caller, "rev-parse", target_branch).stdout.strip()
        if branch_head != base_sha:
            raise IntegrityError("Existing worktree branch points to another base")
        argv = ("worktree", "add", str(target), target_branch)
    else:
        argv = ("worktree", "add", "-b", target_branch, str(target), base_sha)
    created = run_git(caller, *argv, check=False)
    if created.returncode != 0:
        raise IntegrityError(created.stderr.strip() or "Unable to create worktree")
    try:
        _copy_metadata(caller, target, change_id, replace_checkout_snapshot=True)
        bind_owner(caller, change_id, target)
    except Exception:
        run_git(caller, "worktree", "remove", "--force", str(target), check=False)
        raise
    return {
        "ok": True,
        "dry_run": False,
        "created": True,
        "change_id": change_id,
        "base_sha": base_sha,
        "branch": target_branch,
        "owner": str(target),
    }
