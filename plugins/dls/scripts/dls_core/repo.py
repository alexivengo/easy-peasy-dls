"""Repository, Git, profile, template, and trusted-command helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError, IntegrityError
from .io import canonical_file_digest, canonical_text, safe_resolve, sha256_bytes, sha256_file

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
ASSETS_ROOT = PLUGIN_ROOT / "assets"
TEMPLATES_ROOT = ASSETS_ROOT / "templates"
SCHEMAS_ROOT = ASSETS_ROOT / "schemas"
PROFILES_ROOT = ASSETS_ROOT / "profiles"
PROFILE_CONTRACT = "dls-platform-profile/v2"
PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
COMMAND_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PLACEHOLDER = re.compile(r"\{\{[A-Z0-9_]+\}\}")
PROFILE_KEYS = {"schema_version", "name", "extends", "discovery", "evidence", "routing"}
SECTION_KEYS = {
    "discovery": {"hints"},
    "evidence": {"common_types", "platform_types"},
    "routing": {
        "domain_capabilities",
        "domain_skills",
        "domain_skills_are_advisory",
        "process_owner",
    },
}


def find_repo_root(start: Path) -> Path:
    cursor = start.resolve()
    if cursor.is_file():
        cursor = cursor.parent
    for candidate in (cursor, *cursor.parents):
        if (candidate / ".dls" / "config.toml").is_file() or (candidate / ".git").exists():
            return candidate
    return cursor


def _profile_path(root: Path, name: str) -> tuple[Path, str]:
    if not PROFILE_NAME.fullmatch(name):
        raise ConfigError("default_profile must be a safe profile name")
    local = root / ".dls" / "profiles" / f"{name}.toml"
    if local.is_symlink():
        raise ConfigError("Repository profile must not be a symlink")
    if local.is_file():
        return local, "repository"
    bundled = PROFILES_ROOT / f"{name}.toml"
    if bundled.is_file():
        return bundled, "bundled"
    raise ConfigError(f"Unknown DLS profile: {name}")


def _read_profile(root: Path, name: str) -> tuple[dict[str, Any], str]:
    path, source = _profile_path(root, name)
    if path.stat().st_size > 64 * 1024:
        raise ConfigError(f"Profile is too large: {name}")
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Invalid profile {name}: {exc}") from exc
    if set(value) - PROFILE_KEYS or value.get("schema_version") != 1 or value.get("name") != name:
        raise ConfigError(f"Invalid profile contract: {name}")
    for section, allowed in SECTION_KEYS.items():
        table = value.get(section, {})
        if not isinstance(table, dict) or set(table) - allowed:
            raise ConfigError(f"Invalid profile section: {name}.{section}")
    routing = value.get("routing", {})
    if routing.get("domain_skills_are_advisory", True) is not True:
        raise ConfigError("Domain skills must remain advisory")
    if routing.get("process_owner", "dls") != "dls":
        raise ConfigError("Profile process_owner must be dls")
    for section, keys in SECTION_KEYS.items():
        for key in keys:
            item = value.get(section, {}).get(key)
            if item is not None and key not in {"domain_skills_are_advisory", "process_owner"}:
                if not isinstance(item, list) or len(item) > 64 or not all(
                    isinstance(child, str) and 0 < len(child.encode()) <= 512 for child in item
                ):
                    raise ConfigError(f"Profile {name}.{section}.{key} must be a bounded string array")
    return value, source


def resolve_profile(root: Path, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    effective = config
    if effective is None:
        effective = tomllib.loads((root / ".dls" / "config.toml").read_text(encoding="utf-8"))
    selected = effective.get("default_profile", "generic")
    generic, generic_source = _read_profile(root, "generic")
    overlay = generic
    overlay_source = generic_source
    if selected != "generic":
        overlay, overlay_source = _read_profile(root, selected)
        if overlay.get("extends") not in {None, "generic"}:
            raise ConfigError("Profiles may extend only generic")
    elif generic.get("extends") is not None:
        raise ConfigError("generic profile cannot extend another profile")

    def values(payload: dict[str, Any], section: str, key: str) -> list[str]:
        item = payload.get(section, {}).get(key, [])
        return list(item) if isinstance(item, list) else []

    def merged(section: str, key: str) -> list[str]:
        return list(dict.fromkeys([*values(generic, section, key), *values(overlay, section, key)]))

    projection = {
        "contract": PROFILE_CONTRACT,
        "name": selected,
        "source": overlay_source,
        "overlay": None if selected == "generic" else selected,
        "discovery_hints": merged("discovery", "hints"),
        "common_evidence_types": merged("evidence", "common_types"),
        "platform_evidence_types": merged("evidence", "platform_types"),
        "domain_capabilities": merged("routing", "domain_capabilities"),
        "domain_skills": merged("routing", "domain_skills"),
        "domain_skills_are_advisory": True,
        "process_owner": "dls",
    }
    return {**projection, "digest": sha256_bytes(json.dumps(projection, sort_keys=True).encode())}


def load_config(root: Path) -> dict[str, Any]:
    path = root / ".dls" / "config.toml"
    if not path.is_file():
        raise ConfigError(f"DLS is not initialized: {path}")
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid DLS config: {exc}") from exc
    if value.get("schema_version") != 1:
        raise ConfigError("Unsupported config schema")
    docs_root = value.get("docs_root")
    if not isinstance(docs_root, str) or not docs_root:
        raise ConfigError("docs_root must be a relative path")
    safe_resolve(root, docs_root)
    commands = value.get("commands", {})
    policy = value.get("policy", {})
    if not isinstance(commands, dict) or not isinstance(policy, dict):
        raise ConfigError("commands and policy must be tables")
    for key in ("review_required_commands", "acceptance_required_commands"):
        ids = policy.get(key, [])
        if not isinstance(ids, list) or len(ids) != len(set(ids)) or not all(
            isinstance(item, str) and COMMAND_ID.fullmatch(item) for item in ids
        ):
            raise ConfigError(f"policy.{key} must contain unique command IDs")
        unknown = sorted(set(ids) - set(commands))
        if unknown:
            raise ConfigError(f"policy.{key} references unknown commands: {', '.join(unknown)}")
    resolve_profile(root, config=value)
    return value


def render_template(name: str, values: dict[str, str]) -> str:
    path = TEMPLATES_ROOT / name
    if not path.is_file():
        raise IntegrityError(f"Missing DLS template: {name}")
    output = path.read_text(encoding="utf-8")
    for key, value in values.items():
        output = output.replace(f"{{{{{key}}}}}", value)
    unresolved = sorted(set(PLACEHOLDER.findall(output)))
    if unresolved:
        raise IntegrityError(f"Unresolved template placeholders: {', '.join(unresolved)}")
    return output


def copy_asset(source: Path, destination: Path) -> None:
    if destination.exists():
        if destination.read_bytes() == source.read_bytes():
            return
        raise IntegrityError(f"Refusing to overwrite existing file: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode:
        raise IntegrityError(result.stderr.strip() or f"Git failed: {' '.join(args)}")
    return result


def is_git_repo(root: Path) -> bool:
    return run_git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0


def git_head(root: Path) -> str | None:
    result = run_git(root, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def _status_paths(root: Path) -> list[str]:
    output = run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    tokens = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4:
            raise IntegrityError("Malformed Git status output")
        paths.append(token[3:])
        if "R" in token[:2] or "C" in token[:2]:
            index += 1
    return paths


def git_source_dirty_paths(root: Path) -> list[str]:
    return sorted(path for path in _status_paths(root) if path != ".dls" and not path.startswith(".dls/"))


def git_changed_files(root: Path, base: str, head: str) -> list[str]:
    return [item for item in run_git(root, "diff", "--name-only", f"{base}..{head}").stdout.splitlines() if item]


def git_product_tree_digest(root: Path, revision: str = "HEAD") -> str | None:
    resolved = run_git(root, "rev-parse", "--verify", f"{revision}^{{commit}}", check=False)
    if resolved.returncode:
        return None
    rows = []
    for token in run_git(root, "ls-tree", "-r", "-z", "--full-tree", resolved.stdout.strip()).stdout.split("\0"):
        if not token:
            continue
        metadata, separator, relative = token.partition("\t")
        if not separator:
            return None
        if relative != ".dls" and not relative.startswith(".dls/"):
            rows.append(f"{metadata}\0{relative}")
    return sha256_bytes("\0".join(sorted(rows)).encode())


def git_source_snapshot_digest(root: Path) -> str | None:
    head = git_head(root)
    if not head:
        return None
    rows = [f"HEAD\0{head}"]
    for relative in _status_paths(root):
        if relative == ".dls" or relative.startswith(".dls/"):
            continue
        path = root / relative
        digest = sha256_file(path) if path.is_file() else "missing"
        rows.append(f"{relative}\0{digest}")
    return sha256_bytes("\n".join(sorted(rows)).encode())


def package_digest(root: Path, artifacts: dict[str, Any]) -> str:
    rows = []
    for name, item in sorted(artifacts.items()):
        path = safe_resolve(root, item["path"], must_exist=True)
        rows.append(f"{item['path']}\0{canonical_file_digest(path)}")
    return sha256_bytes("\n".join(rows).encode())


def package_digest_at_revision(root: Path, artifacts: dict[str, Any], revision: str) -> str | None:
    rows = []
    for _, item in sorted(artifacts.items()):
        relative = item["path"]
        result = run_git(root, "show", f"{revision}:{relative}", check=False)
        if result.returncode:
            return None
        try:
            digest = sha256_bytes(canonical_text(result.stdout).encode())
        except UnicodeError:
            digest = sha256_bytes(result.stdout.encode())
        rows.append(f"{relative}\0{digest}")
    return sha256_bytes("\n".join(rows).encode())


def command_config(root: Path, command_id: str) -> dict[str, Any]:
    config = load_config(root)
    item = config.get("commands", {}).get(command_id)
    if not isinstance(item, dict):
        raise ConfigError(f"Unknown named command: {command_id}")
    argv = item.get("argv")
    cwd = item.get("cwd", ".")
    timeout = item.get("timeout_seconds", 300)
    cap = item.get("max_output_bytes", 131072)
    env_allow = item.get("env_allow", [])
    if not isinstance(argv, list) or not argv or not all(isinstance(value, str) and value for value in argv):
        raise ConfigError(f"commands.{command_id}.argv must be a non-empty string array")
    if not isinstance(cwd, str):
        raise ConfigError(f"commands.{command_id}.cwd must be text")
    safe_resolve(root, cwd, must_exist=True)
    if not isinstance(timeout, int) or not 1 <= timeout <= 86400:
        raise ConfigError(f"commands.{command_id}.timeout_seconds is invalid")
    if not isinstance(cap, int) or not 1024 <= cap <= 10 * 1024 * 1024:
        raise ConfigError(f"commands.{command_id}.max_output_bytes is invalid")
    if not isinstance(env_allow, list) or not all(isinstance(value, str) for value in env_allow):
        raise ConfigError(f"commands.{command_id}.env_allow must be a string array")
    return {"argv": argv, "cwd": cwd, "timeout_seconds": timeout, "max_output_bytes": cap, "env_allow": env_allow}


def command_contract_digest(root: Path, command_id: str) -> str:
    return sha256_bytes(
        json.dumps(
            {"command_id": command_id, **command_config(root, command_id)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def allowed_environment(names: list[str]) -> dict[str, str]:
    output = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR")
        if key in os.environ
    }
    output.update({key: os.environ[key] for key in names if key in os.environ})
    return output
