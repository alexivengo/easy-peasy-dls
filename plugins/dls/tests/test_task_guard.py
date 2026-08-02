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
    def _loader(action: str, *, dirty: bool = False, head: str = "head-1"):
        def load(_cwd: Path, change_id: str) -> dict[str, object]:
            return {
                "ok": True,
                "change_id": change_id,
                "head_sha": head,
                "source_clean": not dirty,
                "next_action": {"id": action},
                "execution_context": {
                    "owner_dirty": dirty,
                    "owner_head": head,
                },
            }

        return load

    def _handle(
        self,
        payload: dict[str, object],
        action: str,
        *,
        dirty: bool = False,
        head: str = "head-1",
    ):
        return task_guard.handle(
            payload,
            plugin_data=self.plugin_data,
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=self._loader(action, dirty=dirty, head=head),
        )

    def _arm(
        self,
        action: str = "continue-implementation",
        *,
        dirty: bool = False,
        head: str = "head-1",
    ) -> None:
        result = self._handle(
            self._payload(
                "UserPromptSubmit",
                prompt="Исправь findings последнего review EPIC-03a.",
            ),
            action,
            dirty=dirty,
            head=head,
        )
        self.assertIsNone(result)
        stored = json.loads(next(self.plugin_data.rglob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual("EPIC-03a", stored["change_id"])

    def test_two_stalled_stops_then_visible_bounded_diagnostic(self) -> None:
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
        self.assertEqual("block", third["decision"])
        self.assertIn("dls-auto-continuation-exhausted", third["reason"])

        fourth = self._handle(
            self._payload("Stop", stop_hook_active=True, last_assistant_message="diagnosis"),
            "continue-implementation",
        )
        self.assertIsNone(fourth)
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_clean_start_treats_agent_created_dirty_draft_as_non_terminal(self) -> None:
        self._arm(dirty=False)
        result = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="checkpoint"),
            "commit-owner-source",
            dirty=True,
        )
        self.assertEqual("block", result["decision"])
        self.assertIn("commit-owner-source", result["reason"])

    def test_dirty_owner_consent_yes_rearms_guard(self) -> None:
        self._arm("commit-owner-source", dirty=True)
        boundary = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="need consent"),
            "commit-owner-source",
            dirty=True,
        )
        self.assertIsNone(boundary)
        stored = json.loads(next(self.plugin_data.rglob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual("awaiting-owner-consent", stored["state"])

        self.assertIsNone(
            self._handle(
                self._payload("UserPromptSubmit", prompt="Да"),
                "commit-owner-source",
                dirty=True,
            )
        )
        resumed = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="checkpoint"),
            "commit-owner-source",
            dirty=True,
        )
        self.assertEqual("block", resumed["decision"])

    def test_dirty_owner_consent_no_clears_guard(self) -> None:
        self._arm("commit-owner-source", dirty=True)
        self.assertIsNone(
            self._handle(
                self._payload("Stop", stop_hook_active=False, last_assistant_message="need consent"),
                "commit-owner-source",
                dirty=True,
            )
        )
        self.assertIsNone(
            self._handle(
                self._payload("UserPromptSubmit", prompt="Нет"),
                "commit-owner-source",
                dirty=True,
            )
        )
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_unexpected_yes_does_not_arm_guard(self) -> None:
        self.assertIsNone(
            self._handle(
                self._payload("UserPromptSubmit", prompt="Да"),
                "continue-implementation",
            )
        )
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_changed_draft_does_not_reuse_stale_consent(self) -> None:
        self._arm("commit-owner-source", dirty=True, head="head-1")
        self.assertIsNone(
            self._handle(
                self._payload("Stop", stop_hook_active=False, last_assistant_message="need consent"),
                "commit-owner-source",
                dirty=True,
                head="head-1",
            )
        )
        result = self._handle(
            self._payload("UserPromptSubmit", prompt="Да"),
            "commit-owner-source",
            dirty=True,
            head="head-2",
        )
        self.assertEqual("block", result["decision"])
        self.assertIn("dls-owner-consent-stale", result["reason"])
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_progress_resets_stalled_stop_budget(self) -> None:
        self._arm(head="head-1")
        first = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="checkpoint"),
            "continue-implementation",
            head="head-1",
        )
        self.assertEqual("block", first["decision"])
        progressed = self._handle(
            self._payload("Stop", stop_hook_active=True, last_assistant_message="checkpoint 2"),
            "continue-implementation",
            head="head-2",
        )
        self.assertEqual("block", progressed["decision"])
        stored = json.loads(next(self.plugin_data.rglob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(0, stored["stalled_count"])

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
        self._arm("commit-owner-source", dirty=True)
        result = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="need consent"),
            "commit-owner-source",
            dirty=True,
        )
        self.assertIsNone(result)
        binding = json.loads(next(self.plugin_data.rglob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual("awaiting-owner-consent", binding["state"])

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

    def test_real_dirty_owner_consent_rearms_until_commit(self) -> None:
        root = self.plugin_data / "dirty-repo"
        root.mkdir()
        repository(root)
        (root / "src.py").write_text("draft = 0\n", encoding="utf-8")
        commit(root, "product baseline")
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
        (root / "src.py").write_text("draft = 1\n", encoding="utf-8")
        guard_data = self.plugin_data / "dirty-guard"

        def owner_status(cwd: Path, change_id: str) -> dict[str, object]:
            value = status(cwd, change_id)
            value["source_clean"] = False
            value["next_action"] = {"id": "commit-owner-source"}
            value["execution_context"] = {
                "owner_dirty": True,
                "owner_head": git(root, "rev-parse", "HEAD"),
                "owner_root": str(root),
            }
            return value

        prompt = {**self._payload("UserPromptSubmit", prompt="Реализуй C001."), "cwd": str(root)}
        self.assertIsNone(
            task_guard.handle(
                prompt,
                plugin_data=guard_data,
                continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
                status_loader=owner_status,
            )
        )
        stop = {
            **self._payload("Stop", stop_hook_active=False, last_assistant_message="need consent"),
            "cwd": str(root),
        }
        self.assertIsNone(
            task_guard.handle(
                stop,
                plugin_data=guard_data,
                continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
                status_loader=owner_status,
            )
        )
        yes = {**self._payload("UserPromptSubmit", prompt="Да"), "cwd": str(root)}
        self.assertIsNone(
            task_guard.handle(
                yes,
                plugin_data=guard_data,
                continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
                status_loader=owner_status,
            )
        )
        continued = task_guard.handle(
            stop,
            plugin_data=guard_data,
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=owner_status,
        )
        self.assertEqual("block", continued["decision"])
        self.assertIn("commit-owner-source", continued["reason"])
        (root / "src.py").write_text("draft = 2\n", encoding="utf-8")
        progressed = task_guard.handle(
            stop,
            plugin_data=guard_data,
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=owner_status,
        )
        self.assertEqual("block", progressed["decision"])
        binding = json.loads(next(guard_data.rglob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(0, binding["stalled_count"])


if __name__ == "__main__":
    unittest.main()
