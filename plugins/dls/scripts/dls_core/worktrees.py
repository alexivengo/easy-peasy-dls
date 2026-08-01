"""Minimal change-to-worktree ownership backed by Git's own worktree list."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .core import load_state, validate_change_id
from .errors import IntegrityError, UsageError
from .io import FileLock, atomic_write_json, read_json
from .repo import git_head, is_git_repo, run_git

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
    validate_change_id(change_id)
    caller = git_toplevel(root)
    registry = _load_registry(caller)
    identity = registry["owners"].get(change_id)
    if identity is not None:
        if not isinstance(identity, str) or Path(identity).is_absolute() or ".." in Path(identity).parts:
            raise IntegrityError(f"Invalid worktree identity for {change_id}")
        owner = _worktrees(caller).get(identity)
        if owner is None:
            raise IntegrityError(f"Owner worktree is unavailable for {change_id}")
        load_state(owner, change_id, allow_legacy=True)
        return owner
    if (caller / ".dls" / "state" / f"{change_id}.json").is_file():
        return caller
    raise IntegrityError(f"No owner worktree prepared for {change_id}")


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


def _copy_metadata(source: Path, target: Path, change_id: str) -> None:
    (target / ".dls" / "state").mkdir(parents=True, exist_ok=True)
    source_state = source / ".dls" / "state" / f"{change_id}.json"
    if not source_state.is_file():
        raise IntegrityError(f"Missing DLS state for {change_id}")
    target_state = target / ".dls" / "state" / source_state.name
    if target_state.is_file() and target_state.read_bytes() != source_state.read_bytes():
        raise IntegrityError(f"Existing owner state differs for {change_id}")
    source_config = source / ".dls" / "config.toml"
    target_config = target / ".dls" / "config.toml"
    if target_config.is_file() and target_config.read_bytes() != source_config.read_bytes():
        raise IntegrityError("Existing owner config differs")
    if not target_config.exists():
        shutil.copy2(source_config, target_config)
    if not target_state.exists():
        shutil.copy2(source_state, target_state)
    profile_dir = source / ".dls" / "profiles"
    if profile_dir.is_dir():
        target_profiles = target / ".dls" / "profiles"
        for profile in profile_dir.glob("*.toml"):
            target_profile = target_profiles / profile.name
            if target_profile.is_file() and target_profile.read_bytes() != profile.read_bytes():
                raise IntegrityError(f"Existing owner profile differs: {profile.name}")
            target_profiles.mkdir(parents=True, exist_ok=True)
            if not target_profile.exists():
                shutil.copy2(profile, target_profile)


def prepare(
    root: Path,
    *,
    change_id: str,
    base: str,
    path: Path | None,
    branch: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    caller = git_toplevel(root)
    validate_change_id(change_id)
    resolved = run_git(caller, "rev-parse", "--verify", f"{base}^{{commit}}", check=False)
    base_sha = resolved.stdout.strip()
    if resolved.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", base_sha):
        raise UsageError(f"Unknown worktree base: {base}")
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
    if target.exists():
        registered = set(_worktrees(caller).values())
        if target not in registered:
            raise IntegrityError(f"Worktree path already exists: {target}")
        owner_branch = run_git(target, "branch", "--show-current").stdout.strip()
        if owner_branch != target_branch:
            raise IntegrityError("Existing worktree branch does not match requested branch")
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
    created = run_git(
        caller,
        "worktree",
        "add",
        "-b",
        target_branch,
        str(target),
        base_sha,
        check=False,
    )
    if created.returncode != 0:
        raise IntegrityError(created.stderr.strip() or "Unable to create worktree")
    try:
        _copy_metadata(caller, target, change_id)
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
