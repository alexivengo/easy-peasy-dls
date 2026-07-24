"""Repository, configuration, template, and Git helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError, IntegrityError
from .io import canonical_file_digest, safe_resolve, sha256_bytes, sha256_file

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
ASSETS_ROOT = PLUGIN_ROOT / "assets"
TEMPLATES_ROOT = ASSETS_ROOT / "templates"
SCHEMAS_ROOT = ASSETS_ROOT / "schemas"
PROFILES_ROOT = ASSETS_ROOT / "profiles"
PLACEHOLDER_PATTERN = re.compile(r"\{\{[A-Z0-9_]+\}\}")


def find_repo_root(start: Path) -> Path:
    cursor = start.resolve()
    if cursor.is_file():
        cursor = cursor.parent
    for candidate in (cursor, *cursor.parents):
        if (candidate / ".dls" / "config.toml").is_file():
            return candidate
        if (candidate / ".git").exists():
            return candidate
    return cursor


def load_config(root: Path) -> dict[str, Any]:
    path = root / ".dls" / "config.toml"
    if not path.is_file():
        raise ConfigError(f"DLS is not initialized: {path}")
    try:
        config = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid DLS config {path}: {exc}") from exc
    if config.get("schema_version") != 1:
        raise ConfigError(f"Unsupported config schema: {config.get('schema_version')!r}")
    docs_root = config.get("docs_root")
    if not isinstance(docs_root, str) or not docs_root:
        raise ConfigError("config.docs_root must be a non-empty relative path")
    safe_resolve(root, docs_root)
    profile = config.get("default_profile")
    if not isinstance(profile, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", profile):
        raise ConfigError("config.default_profile must be a safe profile name")
    repository_profile = root / ".dls" / "profiles" / f"{profile}.toml"
    bundled_profile = PROFILES_ROOT / f"{profile}.toml"
    if not repository_profile.is_file() and not bundled_profile.is_file():
        raise ConfigError(f"Unknown DLS profile: {profile}")
    policy = config.get("policy", {})
    if not isinstance(policy, dict):
        raise ConfigError("config.policy must be a table")
    for key in ("review_required_commands", "acceptance_required_commands"):
        command_ids = policy.get(key, [])
        if not isinstance(command_ids, list) or not all(
            isinstance(command_id, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", command_id)
            for command_id in command_ids
        ):
            raise ConfigError(f"policy.{key} must be a string array of command IDs")
        if len(command_ids) != len(set(command_ids)):
            raise ConfigError(f"policy.{key} must not contain duplicates")
        unknown = sorted(set(command_ids) - set(config.get("commands", {})))
        if unknown:
            raise ConfigError(
                f"policy.{key} references unknown commands: {', '.join(unknown)}"
            )
    return config


def render_template(name: str, values: dict[str, str]) -> str:
    template_path = TEMPLATES_ROOT / name
    if not template_path.is_file():
        raise IntegrityError(f"Missing DLS template: {name}")
    rendered = template_path.read_text(encoding="utf-8")
    for key, value in values.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    unresolved = sorted(set(PLACEHOLDER_PATTERN.findall(rendered)))
    if unresolved:
        raise IntegrityError(f"Unresolved template placeholders in {name}: {', '.join(unresolved)}")
    return rendered


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
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise IntegrityError(f"Git command failed: git {' '.join(args)}: {detail}")
    return result


def is_git_repo(root: Path) -> bool:
    return run_git(root, "rev-parse", "--is-inside-work-tree", check=False).returncode == 0


def git_head(root: Path) -> str | None:
    result = run_git(root, "rev-parse", "HEAD", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def git_merge_base(root: Path, base: str, head: str) -> str:
    return run_git(root, "merge-base", base, head).stdout.strip()


def git_changed_files(root: Path, base: str, head: str) -> list[str]:
    output = run_git(root, "diff", "--name-only", f"{base}..{head}").stdout
    return [line for line in output.splitlines() if line]


def git_source_dirty_paths(root: Path) -> list[str]:
    paths: list[str] = []
    for _, path in _git_status_entries(root):
        if path == ".dls" or path.startswith(".dls/"):
            continue
        paths.append(path)
    return paths


def git_source_snapshot_digest(root: Path) -> str | None:
    head = git_head(root)
    if not head:
        return None
    entries = [f"HEAD\0{head}"]
    for status_code, relative in _git_status_entries(root):
        if relative == ".dls" or relative.startswith(".dls/"):
            continue
        path = root / relative
        if path.is_symlink():
            content_digest = sha256_bytes(os.readlink(path).encode("utf-8"))
        elif path.is_file():
            content_digest = sha256_file(path)
        elif path.exists():
            content_digest = "non-file"
        else:
            content_digest = "deleted"
        entries.append(f"{status_code}\0{relative}\0{content_digest}")
    return sha256_bytes("\n".join(sorted(entries)).encode("utf-8"))


def _git_status_entries(root: Path) -> list[tuple[str, str]]:
    output = run_git(
        root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout
    tokens = output.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if len(token) < 4:
            raise IntegrityError("Malformed Git status output")
        status_code = token[:2]
        path = token[3:]
        entries.append((status_code, path))
        if "R" in status_code or "C" in status_code:
            index += 1
    return entries


def package_digest(root: Path, artifacts: dict[str, Any]) -> str:
    entries: list[str] = []
    for name, metadata in sorted(artifacts.items()):
        if not isinstance(metadata, dict) or not isinstance(metadata.get("path"), str):
            raise IntegrityError(f"Invalid artifact metadata: {name}")
        relative = metadata["path"]
        path = safe_resolve(root, relative, must_exist=True)
        producer_ticket_scope = metadata.get("producer_ticket_scope")
        if producer_ticket_scope is None:
            content_digest = canonical_file_digest(path)
        else:
            if not isinstance(producer_ticket_scope, list) or not all(
                isinstance(ticket_id, str) and ticket_id
                for ticket_id in producer_ticket_scope
            ):
                raise IntegrityError(
                    f"Invalid producer_ticket_scope metadata: {name}"
                )
            content_digest = _scoped_traceability_digest(
                path,
                set(producer_ticket_scope),
            )
        entries.append(f"{relative}\0{content_digest}")
    from .io import sha256_bytes

    return sha256_bytes("\n".join(entries).encode("utf-8"))


def _scoped_traceability_digest(path: Path, producer_tickets: set[str]) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise IntegrityError(f"Malformed scoped traceability JSON {path}: {exc}") from exc
    scoped_rows: list[dict[str, Any]] = []

    def visit(value: Any, trail: tuple[str, ...]) -> None:
        if isinstance(value, dict):
            producer = value.get("producerTicket")
            if isinstance(producer, str) and producer in producer_tickets:
                scoped_rows.append(
                    {
                        "path": list(trail),
                        "value": value,
                    }
                )
                return
            for key, child in value.items():
                visit(child, (*trail, str(key)))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*trail, str(index)))

    visit(payload, ())
    canonical = json.dumps(
        sorted(scoped_rows, key=lambda item: item["path"]),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return sha256_bytes(canonical.encode("utf-8"))


def command_config(root: Path, command_id: str) -> dict[str, Any]:
    config = load_config(root)
    commands = config.get("commands", {})
    if not isinstance(commands, dict) or command_id not in commands:
        raise ConfigError(f"Unknown named command: {command_id}")
    command = commands[command_id]
    if not isinstance(command, dict):
        raise ConfigError(f"commands.{command_id} must be a table")
    argv = command.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise ConfigError(f"commands.{command_id}.argv must be a non-empty string array")
    cwd = command.get("cwd", ".")
    if not isinstance(cwd, str):
        raise ConfigError(f"commands.{command_id}.cwd must be a string")
    safe_resolve(root, cwd, must_exist=True)
    timeout = command.get("timeout_seconds", 300)
    cap = command.get("max_output_bytes", 131072)
    if not isinstance(timeout, int) or not 1 <= timeout <= 86400:
        raise ConfigError(f"commands.{command_id}.timeout_seconds is out of range")
    if not isinstance(cap, int) or not 1024 <= cap <= 10 * 1024 * 1024:
        raise ConfigError(f"commands.{command_id}.max_output_bytes is out of range")
    env_allow = command.get("env_allow", [])
    if not isinstance(env_allow, list) or not all(isinstance(item, str) for item in env_allow):
        raise ConfigError(f"commands.{command_id}.env_allow must be a string array")
    return {
        "argv": argv,
        "cwd": cwd,
        "timeout_seconds": timeout,
        "max_output_bytes": cap,
        "env_allow": env_allow,
    }


def allowed_environment(names: list[str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    for key in names:
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment
