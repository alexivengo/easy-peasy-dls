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
PROFILE_CONTRACT = "dls-platform-profile/v1"
PROFILE_NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
PROFILE_VALUE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
PROFILE_TYPE_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
PROFILE_MAX_BYTES = 64 * 1024
PROFILE_MAX_DEPTH = 8
PROFILE_MAX_LIST_ITEMS = 64
PROFILE_MAX_TEXT_BYTES = 512
PROFILE_MAX_PROJECTION_BYTES = 16 * 1024

_PROFILE_TOP_LEVEL_KEYS = {"schema_version", "name", "extends", "discovery", "evidence", "routing"}
_PROFILE_SECTION_KEYS = {
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
    if not isinstance(profile, str) or not PROFILE_NAME_PATTERN.fullmatch(profile):
        raise ConfigError("config.default_profile must be a safe profile name")
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
    resolve_profile(root, config=config)
    return config


def _profile_source(root: Path, name: str) -> tuple[Path, str]:
    if not PROFILE_NAME_PATTERN.fullmatch(name):
        raise ConfigError(f"Invalid DLS profile name: {name!r}")
    relative = Path(".dls") / "profiles" / f"{name}.toml"
    repository_candidate = root.resolve() / relative
    if repository_candidate.is_symlink():
        raise ConfigError(f"DLS profile must not be a symlink: {name}")
    repository_profile = safe_resolve(root, relative)
    if repository_profile.is_file():
        return repository_profile, "repository"
    bundled_profile = PROFILES_ROOT / f"{name}.toml"
    if bundled_profile.is_file():
        return bundled_profile, "bundled"
    raise ConfigError(f"Unknown DLS profile: {name}")


def _profile_string_list(
    value: object,
    *,
    location: str,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > PROFILE_MAX_LIST_ITEMS:
        raise ConfigError(
            f"{location} must be an array with at most {PROFILE_MAX_LIST_ITEMS} items"
        )
    output: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item.encode("utf-8")) > PROFILE_MAX_TEXT_BYTES
            or (pattern is not None and not pattern.fullmatch(item))
        ):
            raise ConfigError(f"{location} contains an invalid string")
        if item not in output:
            output.append(item)
    return output


def _read_profile(root: Path, name: str) -> tuple[dict[str, Any], str]:
    path, source = _profile_source(root, name)
    if path.is_symlink():
        raise ConfigError(f"DLS profile must not be a symlink: {name}")
    if path.stat().st_size > PROFILE_MAX_BYTES:
        raise ConfigError(f"DLS profile exceeds {PROFILE_MAX_BYTES} bytes: {name}")
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Invalid DLS profile {name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"DLS profile must be a TOML table: {name}")
    unknown_top = sorted(set(payload) - _PROFILE_TOP_LEVEL_KEYS)
    if unknown_top:
        raise ConfigError(
            f"DLS profile {name} has unknown fields: {', '.join(unknown_top)}"
        )
    if payload.get("schema_version") != 1:
        raise ConfigError(f"Unsupported DLS profile schema: {payload.get('schema_version')!r}")
    if payload.get("name") != name:
        raise ConfigError(f"DLS profile name must match its filename: {name}")
    parent = payload.get("extends")
    if parent is not None and (
        not isinstance(parent, str) or not PROFILE_NAME_PATTERN.fullmatch(parent)
    ):
        raise ConfigError(f"DLS profile {name}.extends must be a safe profile name")

    for section_name, allowed_keys in _PROFILE_SECTION_KEYS.items():
        section = payload.get(section_name, {})
        if not isinstance(section, dict):
            raise ConfigError(f"DLS profile {name}.{section_name} must be a table")
        unknown = sorted(set(section) - allowed_keys)
        if unknown:
            raise ConfigError(
                f"DLS profile {name}.{section_name} has unknown fields: "
                + ", ".join(unknown)
            )

    discovery = payload.get("discovery", {})
    evidence = payload.get("evidence", {})
    routing = payload.get("routing", {})
    advisory = routing.get("domain_skills_are_advisory")
    if advisory is not None and advisory is not True:
        raise ConfigError("DLS profile domain skills must remain advisory")
    process_owner = routing.get("process_owner")
    if process_owner is not None and process_owner != "dls":
        raise ConfigError("DLS profile process_owner must be dls")
    normalized = {
        "name": name,
        "extends": parent,
        "discovery_hints": _profile_string_list(
            discovery.get("hints"),
            location=f"profile.{name}.discovery.hints",
        ),
        "common_evidence_types": _profile_string_list(
            evidence.get("common_types"),
            location=f"profile.{name}.evidence.common_types",
            pattern=PROFILE_TYPE_PATTERN,
        ),
        "platform_evidence_types": _profile_string_list(
            evidence.get("platform_types"),
            location=f"profile.{name}.evidence.platform_types",
            pattern=PROFILE_TYPE_PATTERN,
        ),
        "domain_capabilities": _profile_string_list(
            routing.get("domain_capabilities"),
            location=f"profile.{name}.routing.domain_capabilities",
            pattern=PROFILE_TYPE_PATTERN,
        ),
        "domain_skills": _profile_string_list(
            routing.get("domain_skills"),
            location=f"profile.{name}.routing.domain_skills",
            pattern=PROFILE_VALUE_PATTERN,
        ),
        "domain_skills_are_advisory": advisory,
        "process_owner": process_owner,
    }
    return normalized, source


def _extend_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def resolve_profile(
    root: Path,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one bounded repository-selected platform profile."""

    effective_config = config
    if effective_config is None:
        path = root / ".dls" / "config.toml"
        if not path.is_file():
            raise ConfigError(f"DLS is not initialized: {path}")
        try:
            effective_config = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"Invalid DLS config {path}: {exc}") from exc
    selected = effective_config.get("default_profile")
    if not isinstance(selected, str) or not PROFILE_NAME_PATTERN.fullmatch(selected):
        raise ConfigError("config.default_profile must be a safe profile name")

    chain: list[tuple[dict[str, Any], str]] = []
    seen: set[str] = set()
    current: str | None = selected
    while current is not None:
        if current in seen:
            raise ConfigError(f"DLS profile inheritance cycle includes: {current}")
        if len(chain) >= PROFILE_MAX_DEPTH:
            raise ConfigError(
                f"DLS profile inheritance exceeds depth {PROFILE_MAX_DEPTH}"
            )
        seen.add(current)
        document, source = _read_profile(root, current)
        chain.append((document, source))
        current = document["extends"]

    merged = {
        "discovery_hints": [],
        "common_evidence_types": [],
        "platform_evidence_types": [],
        "domain_capabilities": [],
        "domain_skills": [],
        "domain_skills_are_advisory": True,
        "process_owner": "dls",
    }
    for document, _ in reversed(chain):
        for key in (
            "discovery_hints",
            "common_evidence_types",
            "platform_evidence_types",
            "domain_capabilities",
            "domain_skills",
        ):
            _extend_unique(merged[key], document[key])
        if document["domain_skills_are_advisory"] is not None:
            merged["domain_skills_are_advisory"] = document[
                "domain_skills_are_advisory"
            ]
        if document["process_owner"] is not None:
            merged["process_owner"] = document["process_owner"]
    if merged["domain_skills_are_advisory"] is not True:
        raise ConfigError("Resolved DLS profile domain skills must remain advisory")
    if merged["process_owner"] != "dls":
        raise ConfigError("Resolved DLS profile process_owner must be dls")

    resolved = {
        "contract": PROFILE_CONTRACT,
        "name": selected,
        "source": chain[0][1],
        "inheritance_chain": [document["name"] for document, _ in reversed(chain)],
        **merged,
    }
    digest_basis = {
        key: value
        for key, value in resolved.items()
        if key not in {"source", "inheritance_chain"}
    }
    encoded = json.dumps(
        digest_basis,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > PROFILE_MAX_PROJECTION_BYTES:
        raise ConfigError(
            f"Resolved DLS profile exceeds {PROFILE_MAX_PROJECTION_BYTES} bytes"
        )
    return {**resolved, "digest": sha256_bytes(encoded)}


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


def git_product_tree_digest(root: Path, revision: str = "HEAD") -> str | None:
    """Return a stable digest of tracked product files at ``revision``.

    DLS artifacts are deliberately excluded.  Unlike
    :func:`git_source_snapshot_digest`, this digest describes the committed
    product tree rather than an exact commit plus its working-copy drift.  It
    is therefore suitable for proving that a metadata-only descendant commit
    still contains the product revision that a human accepted.
    """
    resolved = run_git(
        root,
        "rev-parse",
        "--verify",
        f"{revision}^{{commit}}",
        check=False,
    )
    commit_sha = resolved.stdout.strip()
    if resolved.returncode != 0 or not commit_sha:
        return None
    result = run_git(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit_sha,
        check=False,
    )
    if result.returncode != 0:
        return None
    entries: list[str] = []
    for token in result.stdout.split("\0"):
        if not token:
            continue
        metadata, separator, relative = token.partition("\t")
        if not separator:
            return None
        if relative == ".dls" or relative.startswith(".dls/"):
            continue
        entries.append(f"{metadata}\0{relative}")
    return sha256_bytes("\0".join(sorted(entries)).encode("utf-8"))


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


def command_contract_digest(root: Path, command_id: str) -> str:
    """Bind reusable evidence to the exact trusted command contract."""
    command = command_config(root, command_id)
    payload = json.dumps(
        {
            "command_id": command_id,
            **command,
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(payload)


def allowed_environment(names: list[str]) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "WINDIR"):
        if key in os.environ:
            environment[key] = os.environ[key]
    for key in names:
        if key in os.environ:
            environment[key] = os.environ[key]
    return environment
