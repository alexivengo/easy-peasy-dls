#!/usr/bin/env python3
"""Bounded Stop guard for explicit DLS implementation turns."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


CONTRACT = "dls-runtime-completion-guard/v3"
MAX_CONTINUATIONS = 2
BINDING_TTL_SECONDS = 24 * 60 * 60
GIT_DEADLINE_SECONDS = 4.0
MAX_GIT_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_UNTRACKED_BYTES = 16 * 1024 * 1024
CHANGE_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._-])([A-Z][A-Za-z0-9._-]{0,63})"
)
IMPLEMENTATION_WORDS = (
    "реализуй",
    "реализовать",
    "исправь",
    "исправить",
    "продолжи",
    "продолжай",
    "implement",
    "fix",
    "remediate",
    "continue",
)
CANCEL_WORDS = ("стоп", "остановись", "отмена", "отмени", "cancel", "stop")
YES_WORDS = ("да", "yes")
NO_WORDS = ("нет", "no")


def _plugin_root() -> Path:
    return Path(os.environ.get("PLUGIN_ROOT") or Path(__file__).resolve().parents[1])


def _core() -> tuple[frozenset[str], Callable[[Path, str], dict[str, Any]]]:
    scripts = _plugin_root() / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from dls_core.cli import build_parser, dispatch
    from dls_core.core import IMPLEMENTATION_CONTINUE_ACTIONS

    def load(cwd: Path, change_id: str) -> dict[str, Any]:
        args = build_parser().parse_args(
            ["--root", str(cwd), "--json", "status", change_id]
        )
        return dispatch(args)

    return IMPLEMENTATION_CONTINUE_ACTIONS, load


def _binding_path(plugin_data: Path, session_id: str) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return plugin_data / "task-guards" / f"{digest}.json"


def _ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError("guard data directory must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise RuntimeError("guard data directory must not be a symlink")


def _write_binding(path: Path, value: dict[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    if path.is_symlink():
        raise RuntimeError("guard binding must not be a symlink")
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _read_binding(path: Path) -> dict[str, Any] | None:
    if path.is_symlink():
        raise RuntimeError("guard binding must not be a symlink")
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("contract") != CONTRACT
        or not isinstance(value.get("change_id"), str)
        or value.get("state") not in {"active", "awaiting-owner-consent"}
        or not isinstance(value.get("continuation_count"), int)
        or not isinstance(value.get("initial_owner_dirty"), bool)
        or not isinstance(value.get("draft_authorized"), bool)
        or not isinstance(value.get("session_hash"), str)
        or not isinstance(value.get("common_dir_hash"), str)
        or not isinstance(value.get("owner_gitdir_hash"), str)
        or not isinstance(value.get("head_hash"), str)
        or not isinstance(value.get("draft_digest"), str)
        or not isinstance(value.get("created_at"), (int, float))
    ):
        raise RuntimeError("invalid guard binding")
    if time.time() - float(value["created_at"]) > BINDING_TTL_SECONDS:
        _clear(path)
        return None
    return value


def _clear(path: Path) -> None:
    if path.is_file() or path.is_symlink():
        path.unlink()


def _is_plan_mode(payload: dict[str, Any]) -> bool:
    return str(payload.get("permission_mode") or "").casefold() == "plan"


def _is_cancel(prompt: str) -> bool:
    normalized = prompt.strip().casefold()
    return any(re.fullmatch(rf"{re.escape(word)}[.!]?", normalized) for word in CANCEL_WORDS)


def _is_answer(prompt: str, words: tuple[str, ...]) -> bool:
    normalized = prompt.strip().casefold()
    return any(re.fullmatch(rf"{re.escape(word)}[.!]?", normalized) for word in words)


def _has_implementation_intent(prompt: str) -> bool:
    normalized = prompt.casefold()
    return any(
        re.search(rf"(?<!\w){re.escape(word)}(?!\w)", normalized)
        for word in IMPLEMENTATION_WORDS
    )


def _candidate_ids(prompt: str) -> list[str]:
    return list(
        dict.fromkeys(value.rstrip(".,;:!?") for value in CHANGE_ID_PATTERN.findall(prompt))
    )


def _status_action(value: dict[str, Any]) -> str | None:
    action = value.get("next_action")
    return action.get("id") if isinstance(action, dict) and isinstance(action.get("id"), str) else None


def _owner_dirty(value: dict[str, Any]) -> bool:
    context = value.get("execution_context")
    if isinstance(context, dict) and isinstance(context.get("owner_dirty"), bool):
        return context["owner_dirty"]
    return value.get("source_clean") is False


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_git(root: Path, arguments: tuple[str, ...], *, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("guard Git deadline exceeded")
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            ["git", "-C", str(root), *arguments],
            stdout=output,
            stderr=subprocess.DEVNULL,
        )
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait()
            raise TimeoutError("guard Git deadline exceeded") from error
        if returncode != 0:
            raise RuntimeError("guard Git command failed")
        size = output.tell()
        if size > MAX_GIT_OUTPUT_BYTES:
            raise RuntimeError("guard Git output exceeded cap")
        output.seek(0)
        return output.read()


def _untracked_digest(root: Path, payload: bytes, *, deadline: float) -> str:
    digest = hashlib.sha256()
    total = 0
    paths = [item for item in payload.split(b"\0") if item]
    for raw in paths:
        if time.monotonic() >= deadline:
            raise TimeoutError("guard draft deadline exceeded")
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("unsafe untracked path")
        path = root / relative
        metadata = path.lstat()
        digest.update(raw)
        digest.update(b"\0")
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path).encode("utf-8", "surrogateescape")
            total += len(target)
            if total > MAX_UNTRACKED_BYTES:
                raise RuntimeError("untracked draft exceeded cap")
            digest.update(b"link\0")
            digest.update(target)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("unsupported untracked draft entry")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            digest.update(b"file\0")
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("guard draft deadline exceeded")
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UNTRACKED_BYTES:
                    raise RuntimeError("untracked draft exceeded cap")
                digest.update(chunk)
        finally:
            os.close(descriptor)
    return digest.hexdigest()


def _owner_snapshot(value: dict[str, Any]) -> dict[str, str]:
    context = value.get("execution_context")
    owner_root = context.get("owner_root") if isinstance(context, dict) else None
    if not isinstance(owner_root, str):
        raise RuntimeError("DLS status lacks owner root")
    root = Path(owner_root)
    if not root.is_dir():
        raise RuntimeError("DLS owner root is unavailable")
    deadline = time.monotonic() + GIT_DEADLINE_SECONDS
    identity = _run_git(
        root,
        ("rev-parse", "--git-common-dir", "--git-dir", "HEAD"),
        deadline=deadline,
    ).decode("utf-8", "strict").splitlines()
    if len(identity) != 3:
        raise RuntimeError("unexpected Git identity output")
    tracked = _run_git(
        root,
        ("diff", "--binary", "--no-ext-diff", "--no-color", "HEAD", "--"),
        deadline=deadline,
    )
    untracked = _run_git(
        root,
        ("ls-files", "--others", "--exclude-standard", "-z"),
        deadline=deadline,
    )
    draft = hashlib.sha256()
    draft.update(tracked)
    draft.update(b"\0")
    draft.update(_untracked_digest(root, untracked, deadline=deadline).encode("ascii"))
    return {
        "common_dir_hash": _hash(str((root / identity[0]).resolve())),
        "owner_gitdir_hash": _hash(str((root / identity[1]).resolve())),
        "head_hash": _hash(identity[2]),
        "draft_digest": draft.hexdigest(),
    }


def _continuation(binding: dict[str, Any], action: str) -> dict[str, Any]:
    count = binding["continuation_count"] + 1
    binding["continuation_count"] = count
    if count > MAX_CONTINUATIONS:
        return {
            "continue": False,
            "stopReason": "dls-auto-continuation-exhausted",
            "systemMessage": (
                "dls-auto-continuation-exhausted: implementation remains incomplete; "
                f"DLS still reports {action} for {binding['change_id']} after "
                f"{MAX_CONTINUATIONS} automatic continuations."
            ),
        }
    return {
        "decision": "block",
        "reason": (
            f"[DLS_CONTINUE {count}/{MAX_CONTINUATIONS}] DLS reports {action} "
            f"for {binding['change_id']}. "
            "Continue the same implementation task. Do not finish with a progress report; "
            "stop only at open-review-task or a proven external blocker."
        ),
    }


def handle(
    payload: dict[str, Any],
    *,
    plugin_data: Path,
    continue_actions: frozenset[str],
    status_loader: Callable[[Path, str], dict[str, Any]],
    snapshot_loader: Callable[[dict[str, Any]], dict[str, str]] = _owner_snapshot,
) -> dict[str, Any] | None:
    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    event = payload.get("hook_event_name")
    if not isinstance(session_id, str) or not session_id or not isinstance(cwd, str):
        raise RuntimeError("hook input lacks session_id or cwd")
    binding_path = _binding_path(plugin_data, session_id)
    try:
        if event == "UserPromptSubmit":
            prompt = payload.get("prompt")
            if not isinstance(prompt, str):
                raise RuntimeError("UserPromptSubmit lacks prompt")
            if prompt.startswith("[DLS_CONTINUE"):
                return None
            if _is_plan_mode(payload) or _is_cancel(prompt):
                _clear(binding_path)
                return None
            binding = _read_binding(binding_path)
            if binding is not None and binding["state"] == "awaiting-owner-consent":
                if _is_answer(prompt, NO_WORDS):
                    _clear(binding_path)
                    return None
                if not _is_answer(prompt, YES_WORDS):
                    _clear(binding_path)
                    return None
                value = status_loader(Path(cwd), binding["change_id"])
                snapshot = snapshot_loader(value)
                expected = {
                    key: binding[key]
                    for key in (
                        "common_dir_hash",
                        "owner_gitdir_hash",
                        "head_hash",
                        "draft_digest",
                    )
                }
                if _status_action(value) != "commit-owner-source" or snapshot != expected:
                    _clear(binding_path)
                    return {
                        "decision": "block",
                        "reason": (
                            "dls-owner-consent-stale: the owner draft or identity changed "
                            "after the consent question. Read DLS status and ask for fresh "
                            "confirmation."
                        ),
                    }
                binding.update(state="active", draft_authorized=True)
                _write_binding(binding_path, binding)
                return None

            # Every explicit user prompt starts a new activation or clears the old one.
            _clear(binding_path)
            if not _has_implementation_intent(prompt):
                return None
            identifiers = _candidate_ids(prompt)
            if len(identifiers) != 1:
                return None
            change_id = identifiers[0]
            value = status_loader(Path(cwd), change_id)
            if value.get("ok") is False or value.get("change_id") != change_id:
                return None
            dirty = _owner_dirty(value)
            snapshot = snapshot_loader(value)
            now = int(time.time())
            _write_binding(
                binding_path,
                {
                    "contract": CONTRACT,
                    "change_id": change_id,
                    "state": "active",
                    "continuation_count": 0,
                    "initial_owner_dirty": dirty,
                    "draft_authorized": not dirty,
                    "role": "implementation",
                    "session_hash": _hash(session_id),
                    "created_at": now,
                    **snapshot,
                },
            )
            return None

        if event != "Stop":
            return None
        binding = _read_binding(binding_path)
        if binding is None:
            return None
        if _is_plan_mode(payload):
            _clear(binding_path)
            return None
        value = status_loader(Path(cwd), binding["change_id"])
        action = _status_action(value)
        if action == "commit-owner-source" and not binding["draft_authorized"]:
            snapshot = snapshot_loader(value)
            expected = {
                key: binding[key]
                for key in (
                    "common_dir_hash",
                    "owner_gitdir_hash",
                    "head_hash",
                    "draft_digest",
                )
            }
            if snapshot != expected:
                _clear(binding_path)
                return {
                    "continue": False,
                    "stopReason": "dls-owner-draft-changed-before-consent",
                    "systemMessage": (
                        "dls-owner-draft-changed-before-consent: the existing owner draft "
                        "changed before permission was granted."
                    ),
                }
            binding.update(state="awaiting-owner-consent")
            _write_binding(binding_path, binding)
            return None
        if action not in continue_actions and action != "commit-owner-source":
            _clear(binding_path)
            return None
        if action == "commit-owner-source":
            action = "continue-implementation"
        result = _continuation(binding, action)
        if result.get("continue") is False:
            _clear(binding_path)
        else:
            _write_binding(binding_path, binding)
        return result
    except Exception as error:
        try:
            _clear(binding_path)
        except Exception:
            pass
        return {"systemMessage": f"dls-task-guard-failed-open: {type(error).__name__}"}


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise RuntimeError("hook input must be an object")
        continue_actions, status_loader = _core()
        plugin_data = Path(os.environ["PLUGIN_DATA"])
        result = handle(
            payload,
            plugin_data=plugin_data,
            continue_actions=continue_actions,
            status_loader=status_loader,
        )
        print(json.dumps(result or {}, ensure_ascii=False, separators=(",", ":")))
    except Exception as error:
        print(
            json.dumps(
                {"systemMessage": f"dls-task-guard-failed-open: {type(error).__name__}"},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
