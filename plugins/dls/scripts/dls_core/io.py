"""Safe filesystem, digest, and redaction primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import IntegrityError, LockError

GENERATED_START = "<!-- DLS:GENERATED:START -->"
GENERATED_END = "<!-- DLS:GENERATED:END -->"
SECRET_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|token|password|secret|dsn)\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_text(text: str) -> str:
    """Normalize authored Markdown and remove generated regions."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    output: list[str] = []
    inside_generated = False
    for line in normalized.split("\n"):
        if line.strip() == GENERATED_START:
            inside_generated = True
            continue
        if line.strip() == GENERATED_END:
            inside_generated = False
            continue
        if not inside_generated:
            output.append(line.rstrip())
    if inside_generated:
        raise IntegrityError("Unclosed DLS generated region")
    return "\n".join(output).rstrip() + "\n"


def canonical_file_digest(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return sha256_file(path)
    return sha256_bytes(canonical_text(text).encode("utf-8"))


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise IntegrityError(f"Missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise IntegrityError(f"Malformed JSON file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise IntegrityError(f"Expected JSON object: {path}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any], *, backup: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        _atomic_write_bytes(backup_path, path.read_bytes())
    _atomic_write_bytes(path, payload)


def atomic_write_text(path: Path, value: str, *, backup: bool = True) -> None:
    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        _atomic_write_bytes(backup_path, path.read_bytes())
    _atomic_write_bytes(path, value.encode("utf-8"))


def write_immutable_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        existing = read_json(path)
        if existing == value:
            return
        raise IntegrityError(f"Immutable artifact already exists with different content: {path}")
    atomic_write_json(path, value, backup=False)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary_path.unlink(missing_ok=True)


def safe_resolve(root: Path, relative: str | Path, *, must_exist: bool = False) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute():
        raise IntegrityError(f"Absolute paths are not allowed: {relative}")
    root_resolved = root.resolve()
    resolved = (root_resolved / candidate).resolve(strict=must_exist)
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise IntegrityError(f"Path escapes repository root: {relative}") from exc
    return resolved


def redact_text(value: str) -> str:
    home = str(Path.home())
    redacted = value.replace(home, "$HOME") if home else value
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


class FileLock(AbstractContextManager["FileLock"]):
    """Exclusive lock with diagnostics; stale locks are never silently removed."""

    def __init__(self, path: Path, *, stale_after_seconds: int = 300) -> None:
        self.path = path
        self.stale_after_seconds = stale_after_seconds
        self._acquired = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "created_at": utc_now(),
            "monotonic": time.monotonic(),
        }
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            age = max(0.0, time.time() - self.path.stat().st_mtime)
            stale = age >= self.stale_after_seconds
            qualifier = "stale" if stale else "active"
            raise LockError(
                f"State lock is {qualifier}: {self.path} (age={age:.1f}s). "
                "Inspect the owner before removing it."
            ) from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._acquired = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._acquired:
            self.path.unlink(missing_ok=True)
            self._acquired = False
