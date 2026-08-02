from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from dls_core.core import (
    IMPLEMENTATION_CONTINUE_ACTIONS,
    approve,
    decision_projection,
    load_state,
    status,
    ticket_set,
)
from dls_core.runner import candidate_ready
from support import change, commit, configure, git, repository


HOOK_PATH = Path(__file__).resolve().parents[1] / "hooks" / "task_guard.py"
SPEC = importlib.util.spec_from_file_location("dls_task_guard", HOOK_PATH)
assert SPEC and SPEC.loader
task_guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(task_guard)


class TaskGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.plugin_data = Path(self.temporary.name)
        self.session_id = "raw-session-id-must-not-leak"
        self.cwd = "/private/disposable-repository"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _payload(self, event: str, **extra: object) -> dict[str, object]:
        return {
            "hook_event_name": event,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "permission_mode": "default",
            "transcript_path": "/private/transcript.jsonl",
            **extra,
        }

    @staticmethod
    def _loader(action: str):
        def load(_cwd: Path, change_id: str) -> dict[str, object]:
            return {
                "ok": True,
                "change_id": change_id,
                "next_action": {"id": action},
            }

        return load

    def _handle(self, payload: dict[str, object], action: str):
        return task_guard.handle(
            payload,
            plugin_data=self.plugin_data,
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=self._loader(action),
        )

    def _arm(self, action: str = "continue-implementation") -> None:
        result = self._handle(
            self._payload(
                "UserPromptSubmit",
                prompt="Исправь findings последнего review EPIC-03a.",
            ),
            action,
        )
        self.assertIsNone(result)
        stored = json.loads(next(self.plugin_data.rglob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual("EPIC-03a", stored["change_id"])

    def test_two_checkpoint_stops_continue_then_third_is_bounded(self) -> None:
        self._arm()
        stop = self._payload("Stop", stop_hook_active=False, last_assistant_message="checkpoint")
        first = self._handle(stop, "continue-implementation")
        self.assertEqual("block", first["decision"])
        self.assertIn("[DLS_CONTINUE]", first["reason"])

        generated = self._payload("UserPromptSubmit", prompt=first["reason"])
        self.assertIsNone(self._handle(generated, "continue-implementation"))
        second = self._handle(
            self._payload("Stop", stop_hook_active=True, last_assistant_message="checkpoint 2"),
            "continue-implementation",
        )
        self.assertEqual("block", second["decision"])

        third = self._handle(
            self._payload("Stop", stop_hook_active=True, last_assistant_message="checkpoint 3"),
            "continue-implementation",
        )
        self.assertNotIn("decision", third)
        self.assertIn("dls-auto-continuation-exhausted", third["systemMessage"])
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_open_review_task_is_terminal_and_clears_binding(self) -> None:
        self._arm()
        result = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="ready"),
            "open-review-task",
        )
        self.assertIsNone(result)
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_plan_review_status_and_generic_prompts_do_not_arm(self) -> None:
        prompts = (
            self._payload(
                "UserPromptSubmit",
                permission_mode="plan",
                prompt="Исправь findings EPIC-03a.",
            ),
            self._payload("UserPromptSubmit", prompt="Проведи code review EPIC-03a."),
            self._payload("UserPromptSubmit", prompt="Какой статус EPIC-03a?"),
            self._payload("UserPromptSubmit", prompt="Объясни этот код."),
            self._payload("UserPromptSubmit", prompt="Покажи fixture EPIC-03a."),
        )
        for payload in prompts:
            with self.subTest(prompt=payload["prompt"]):
                self.assertIsNone(self._handle(payload, "continue-implementation"))
                self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_cancel_prompt_clears_binding(self) -> None:
        self._arm()
        self.assertIsNone(
            self._handle(self._payload("UserPromptSubmit", prompt="Стоп."), "continue-implementation")
        )
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_dirty_owner_human_boundary_is_not_bypassed(self) -> None:
        self._arm()
        result = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="need consent"),
            "commit-owner-source",
        )
        self.assertIsNone(result)
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_status_failure_is_fail_open(self) -> None:
        self._arm()

        def fail(_cwd: Path, _change_id: str) -> dict[str, object]:
            raise RuntimeError("broken status")

        result = task_guard.handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="done"),
            plugin_data=self.plugin_data,
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=fail,
        )
        self.assertNotIn("decision", result)
        self.assertEqual("dls-task-guard-failed-open: RuntimeError", result["systemMessage"])
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_corrupt_binding_fails_open_once_and_is_removed(self) -> None:
        self._arm()
        binding = next(self.plugin_data.rglob("*.json"))
        binding.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "invalid guard binding"):
            task_guard.handle(
                self._payload("Stop", stop_hook_active=False, last_assistant_message="done"),
                plugin_data=self.plugin_data,
                continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
                status_loader=self._loader("continue-implementation"),
            )
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_binding_and_output_do_not_leak_session_transcript_or_paths(self) -> None:
        self._arm()
        binding = next(self.plugin_data.rglob("*.json"))
        stored = binding.read_text(encoding="utf-8")
        result = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="progress"),
            "continue-implementation",
        )
        public = json.dumps(result, ensure_ascii=False)
        for secret in (self.session_id, self.cwd, "/private/transcript.jsonl"):
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, public)

    def test_exactly_one_existing_change_is_required(self) -> None:
        prompt = self._payload(
            "UserPromptSubmit",
            prompt="Исправь EPIC-03a и EPIC-04.",
        )
        self.assertIsNone(self._handle(prompt, "continue-implementation"))
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_real_candidate_handoff_continues_without_second_user_prompt(self) -> None:
        root = self.plugin_data / "repo"
        root.mkdir()
        base = repository(root)
        configure(root)
        change(root)
        commit(root, "definition")
        projection = decision_projection(root, load_state(root, "C001"))
        approve(
            root,
            change_id="C001",
            decision="definition",
            include_design=False,
            include_architecture=False,
            actor="user",
            response=f"definition {projection['definition']['digest'][:12]}",
            git_sha=git(root, "rev-parse", "HEAD"),
            dry_run=False,
        )
        ticket_set(root, change_id="C001", ticket_id="C001-T01", value="implemented", note=None)
        (root / "src.py").write_text("value = 1\n", encoding="utf-8")
        commit(root, "implementation")

        payload = {
            **self._payload("UserPromptSubmit", prompt="Реализуй C001."),
            "cwd": str(root),
        }
        self.assertIsNone(
            task_guard.handle(
                payload,
                plugin_data=self.plugin_data / "guard-data",
                continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
                status_loader=status,
            )
        )
        stop = {
            **self._payload("Stop", stop_hook_active=False, last_assistant_message="checkpoint"),
            "cwd": str(root),
        }
        continued = task_guard.handle(
            stop,
            plugin_data=self.plugin_data / "guard-data",
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=status,
        )
        self.assertEqual("block", continued["decision"])
        self.assertIn("run-candidate-ready", continued["reason"])

        ready = candidate_ready(
            root,
            change_id="C001",
            base=base,
            addressed=[],
            noted=[],
            dry_run=False,
        )
        self.assertEqual("open-review-task", ready["next_action"]["id"])
        self.assertIsNone(
            task_guard.handle(
                stop,
                plugin_data=self.plugin_data / "guard-data",
                continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
                status_loader=status,
            )
        )


if __name__ == "__main__":
    unittest.main()
