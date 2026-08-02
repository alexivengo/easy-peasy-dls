#!/usr/bin/env python3
"""Bounded Stop guard for explicit DLS implementation turns."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable


CONTRACT = "dls-runtime-completion-guard/v1"
MAX_CONTINUATIONS = 2
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
        or not isinstance(value.get("continuation_count"), int)
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
        if prompt.startswith("[DLS_CONTINUE]"):
            return None
        if _is_plan_mode(payload) or _is_cancel(prompt):
            _clear(binding_path)
            return None
        if not _has_implementation_intent(prompt):
            _clear(binding_path)
            return None
        valid: list[str] = []
        for change_id in _candidate_ids(prompt):
            try:
                value = status_loader(Path(cwd), change_id)
            except Exception:
                continue
            if value.get("ok") is not False and value.get("change_id") == change_id:
                valid.append(change_id)
        if len(valid) != 1:
            _clear(binding_path)
            return None
        _write_binding(
            binding_path,
            {
                "contract": CONTRACT,
                "change_id": valid[0],
                "continuation_count": 0,
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
    if _is_plan_mode(payload):
        _clear(binding_path)
        return None
    try:
        value = status_loader(Path(cwd), binding["change_id"])
    except Exception as error:
        _clear(binding_path)
        return {"systemMessage": f"dls-task-guard-failed-open: {type(error).__name__}"}
    action = _status_action(value)
    if action not in continue_actions:
        _clear(binding_path)
        return None
    count = binding["continuation_count"]
    if count >= MAX_CONTINUATIONS:
        _clear(binding_path)
        return {
            "systemMessage": (
                "dls-auto-continuation-exhausted: DLS still reports a non-terminal "
                f"action for {binding['change_id']}; manual diagnosis is required."
            )
        }
    binding["continuation_count"] = count + 1
    _write_binding(binding_path, binding)
    return {
        "decision": "block",
        "reason": (
            f"[DLS_CONTINUE] DLS reports {action} for {binding['change_id']}. "
            "Continue the same implementation task. Do not finish with a progress report; "
            "stop only at open-review-task or a proven external blocker."
        ),
    }


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
