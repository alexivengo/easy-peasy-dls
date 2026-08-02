#!/usr/bin/env python3
"""Bounded Stop guard for explicit DLS implementation turns."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


CONTRACT = "dls-runtime-completion-guard/v2"
MAX_STALLED_CONTINUATIONS = 2
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
    if not path.is_file() or path.is_symlink():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("contract") != CONTRACT
        or not isinstance(value.get("change_id"), str)
        or value.get("state") not in {"active", "awaiting-owner-consent", "exhausted"}
        or not isinstance(value.get("stalled_count"), int)
        or not isinstance(value.get("progress_fingerprint"), str)
        or not isinstance(value.get("initial_owner_dirty"), bool)
        or not isinstance(value.get("draft_authorized"), bool)
    ):
        raise RuntimeError("invalid guard binding")
    return value


def _clear(path: Path) -> None:
    if path.is_file() and not path.is_symlink():
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


def _git_progress_digest(root: Path) -> str | None:
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    commands = (
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ("diff", "--binary", "--no-ext-diff", "--no-color", "--"),
        ("diff", "--cached", "--binary", "--no-ext-diff", "--no-color", "--"),
    )
    for command in commands:
        result = subprocess.run(
            ["git", "-C", str(root), *command],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
        if result.returncode != 0:
            return None
        digest.update(result.stdout)
        digest.update(b"\0")
    return digest.hexdigest()


def _progress_fingerprint(value: dict[str, Any]) -> str:
    context = value.get("execution_context")
    owner_root = context.get("owner_root") if isinstance(context, dict) else None
    git_digest = _git_progress_digest(Path(owner_root)) if isinstance(owner_root, str) else None
    projection = {
        "change_id": value.get("change_id"),
        "state_revision": value.get("state_revision"),
        "head_sha": value.get("head_sha"),
        "candidate_head": value.get("candidate_head"),
        "action": _status_action(value),
        "owner_head": context.get("owner_head") if isinstance(context, dict) else None,
        "owner_dirty": _owner_dirty(value),
        "git": git_digest,
    }
    payload = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _continuation(binding: dict[str, Any], action: str, fingerprint: str) -> dict[str, Any]:
    stalled = binding["stalled_count"] + 1 if fingerprint == binding["progress_fingerprint"] else 0
    binding["progress_fingerprint"] = fingerprint
    binding["stalled_count"] = stalled
    if stalled > MAX_STALLED_CONTINUATIONS:
        binding["state"] = "exhausted"
        return {
            "decision": "block",
            "reason": (
                "[DLS_GUARD_EXHAUSTED] dls-auto-continuation-exhausted: "
                f"DLS still reports {action} for {binding['change_id']} after two "
                "continuations without Git progress. Report this exact diagnostic and "
                "the concrete blocker; do not claim completion."
            ),
        }
    return {
        "decision": "block",
        "reason": (
            f"[DLS_CONTINUE] DLS reports {action} for {binding['change_id']}. "
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
) -> dict[str, Any] | None:
    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    event = payload.get("hook_event_name")
    if not isinstance(session_id, str) or not session_id or not isinstance(cwd, str):
        raise RuntimeError("hook input lacks session_id or cwd")
    binding_path = _binding_path(plugin_data, session_id)

    if event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if not isinstance(prompt, str):
            raise RuntimeError("UserPromptSubmit lacks prompt")
        if prompt.startswith(("[DLS_CONTINUE]", "[DLS_GUARD_EXHAUSTED]")):
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
            fingerprint = _progress_fingerprint(value)
            if (
                _status_action(value) != "commit-owner-source"
                or fingerprint != binding.get("consent_fingerprint")
            ):
                _clear(binding_path)
                return {
                    "decision": "block",
                    "reason": (
                        "dls-owner-consent-stale: the owner draft changed after the consent "
                        "question. Read DLS status and ask for fresh confirmation."
                    ),
                }
            binding.update(
                state="active",
                draft_authorized=True,
                progress_fingerprint=fingerprint,
                stalled_count=0,
            )
            binding.pop("consent_fingerprint", None)
            _write_binding(binding_path, binding)
            return None
        if not _has_implementation_intent(prompt):
            _clear(binding_path)
            return None
        valid: list[tuple[str, dict[str, Any]]] = []
        for change_id in _candidate_ids(prompt):
            try:
                value = status_loader(Path(cwd), change_id)
            except Exception:
                continue
            if value.get("ok") is not False and value.get("change_id") == change_id:
                valid.append((change_id, value))
        if len(valid) != 1:
            _clear(binding_path)
            return None
        change_id, value = valid[0]
        dirty = _owner_dirty(value)
        _write_binding(
            binding_path,
            {
                "contract": CONTRACT,
                "change_id": change_id,
                "state": "active",
                "stalled_count": 0,
                "progress_fingerprint": _progress_fingerprint(value),
                "initial_owner_dirty": dirty,
                "draft_authorized": not dirty,
                "role": "implementation",
            },
        )
        return None

    if event != "Stop":
        return None
    try:
        binding = _read_binding(binding_path)
    except Exception:
        _clear(binding_path)
        raise
    if binding is None:
        return None
    if binding["state"] == "exhausted":
        _clear(binding_path)
        return None
    if _is_plan_mode(payload):
        _clear(binding_path)
        return None
    try:
        value = status_loader(Path(cwd), binding["change_id"])
    except Exception as error:
        _clear(binding_path)
        return {"systemMessage": f"dls-task-guard-failed-open: {type(error).__name__}"}
    action = _status_action(value)
    fingerprint = _progress_fingerprint(value)
    if action == "commit-owner-source" and not binding["draft_authorized"]:
        binding.update(
            state="awaiting-owner-consent",
            consent_fingerprint=fingerprint,
            progress_fingerprint=fingerprint,
            stalled_count=0,
        )
        _write_binding(binding_path, binding)
        return None
    if action not in continue_actions and action != "commit-owner-source":
        _clear(binding_path)
        return None
    result = _continuation(binding, action, fingerprint)
    _write_binding(binding_path, binding)
    return result


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
