from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import time
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
HOOKS_CONFIG = HOOK_PATH.parent / "hooks.json"
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
        draft: str = "draft-1",
        owner: str = "owner-1",
    ):
        return task_guard.handle(
            payload,
            plugin_data=self.plugin_data,
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=self._loader(action, dirty=dirty, head=head),
            snapshot_loader=lambda _value: {
                "common_dir_hash": f"common-{owner}",
                "owner_gitdir_hash": f"gitdir-{owner}",
                "head_hash": f"hashed-{head}",
                "draft_digest": draft,
            },
        )

    def _arm(
        self,
        action: str = "continue-implementation",
        *,
        dirty: bool = False,
        head: str = "head-1",
        draft: str = "draft-1",
        owner: str = "owner-1",
    ) -> None:
        result = self._handle(
            self._payload(
                "UserPromptSubmit",
                prompt="Исправь findings последнего review EPIC-03a.",
            ),
            action,
            dirty=dirty,
            head=head,
            draft=draft,
            owner=owner,
        )
        self.assertIsNone(result)
        stored = json.loads(next(self.plugin_data.rglob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual("EPIC-03a", stored["change_id"])

    def test_two_continuations_then_terminal_bounded_diagnostic(self) -> None:
        self._arm()
        stop = self._payload("Stop", stop_hook_active=False, last_assistant_message="checkpoint")
        first = self._handle(stop, "continue-implementation")
        self.assertEqual("block", first["decision"])
        self.assertIn("[DLS_CONTINUE 1/2]", first["reason"])

        generated = self._payload("UserPromptSubmit", prompt=first["reason"])
        self.assertIsNone(self._handle(generated, "continue-implementation"))
        second = self._handle(
            self._payload("Stop", stop_hook_active=True, last_assistant_message="checkpoint 2"),
            "continue-implementation",
        )
        self.assertEqual("block", second["decision"])
        self.assertIn("[DLS_CONTINUE 2/2]", second["reason"])

        third = self._handle(
            self._payload("Stop", stop_hook_active=True, last_assistant_message="checkpoint 3"),
            "continue-implementation",
        )
        self.assertFalse(third["continue"])
        self.assertEqual("dls-auto-continuation-exhausted", third["stopReason"])
        self.assertIn("implementation remains incomplete", third["systemMessage"])
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_git_churn_never_resets_absolute_budget(self) -> None:
        self._arm(head="head-a")
        stops = []
        for index in range(17):
            stops.append(
                self._handle(
                    self._payload(
                        "Stop",
                        stop_hook_active=index > 0,
                        last_assistant_message=f"checkpoint {index}",
                    ),
                    "continue-implementation",
                    head="head-a" if index % 2 == 0 else "head-b",
                    draft=f"draft-{index}",
                )
            )
            if stops[-1] and stops[-1].get("continue") is False:
                break
        self.assertEqual(3, len(stops))
        self.assertEqual(["block", "block"], [item["decision"] for item in stops[:2]])
        self.assertEqual("dls-auto-continuation-exhausted", stops[2]["stopReason"])

    def test_clean_start_treats_agent_created_dirty_draft_as_non_terminal(self) -> None:
        self._arm(dirty=False)
        result = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="checkpoint"),
            "commit-owner-source",
            dirty=True,
        )
        self.assertEqual("block", result["decision"])
        self.assertIn("continue-implementation", result["reason"])

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
        self.assertIn("continue-implementation", resumed["reason"])
        self.assertNotIn("commit-owner-source", resumed["reason"])

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

    def test_owner_or_draft_change_revokes_consent(self) -> None:
        for field in ("draft", "owner"):
            with self.subTest(field=field):
                data = self.plugin_data / field
                data.mkdir()
                original = self.plugin_data
                self.plugin_data = data
                try:
                    self._arm("commit-owner-source", dirty=True)
                    self.assertIsNone(
                        self._handle(
                            self._payload(
                                "Stop",
                                stop_hook_active=False,
                                last_assistant_message="need consent",
                            ),
                            "commit-owner-source",
                            dirty=True,
                        )
                    )
                    result = self._handle(
                        self._payload("UserPromptSubmit", prompt="Да"),
                        "commit-owner-source",
                        dirty=True,
                        draft="draft-2" if field == "draft" else "draft-1",
                        owner="owner-2" if field == "owner" else "owner-1",
                    )
                    self.assertEqual("block", result["decision"])
                    self.assertIn("dls-owner-consent-stale", result["reason"])
                finally:
                    self.plugin_data = original

    def test_exact_snapshot_detects_tracked_staged_and_untracked_content(self) -> None:
        root = self.plugin_data / "snapshot-repo"
        root.mkdir()
        repository(root)
        tracked = root / "tracked.txt"
        tracked.write_text("one\n", encoding="utf-8")
        commit(root, "baseline")

        def projection() -> dict[str, object]:
            return {"execution_context": {"owner_root": str(root)}}

        clean = task_guard._owner_snapshot(projection())
        tracked.write_text("two\n", encoding="utf-8")
        modified = task_guard._owner_snapshot(projection())
        self.assertNotEqual(clean["draft_digest"], modified["draft_digest"])
        git(root, "add", "tracked.txt")
        staged = task_guard._owner_snapshot(projection())
        self.assertEqual(modified["draft_digest"], staged["draft_digest"])
        extra = root / "new.txt"
        extra.write_text("alpha\n", encoding="utf-8")
        untracked = task_guard._owner_snapshot(projection())
        extra.write_text("bravo\n", encoding="utf-8")
        changed_untracked = task_guard._owner_snapshot(projection())
        self.assertNotEqual(untracked["draft_digest"], changed_untracked["draft_digest"])

    def test_real_progress_does_not_expand_absolute_budget(self) -> None:
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
        self.assertEqual(2, stored["continuation_count"])

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

    def test_dirty_owner_change_before_consent_stops_guard(self) -> None:
        self._arm("commit-owner-source", dirty=True, draft="draft-1")
        result = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="need consent"),
            "commit-owner-source",
            dirty=True,
            draft="draft-2",
        )
        self.assertFalse(result["continue"])
        self.assertEqual("dls-owner-draft-changed-before-consent", result["stopReason"])
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

    def test_snapshot_failure_on_prompt_is_fail_open_and_clears_binding(self) -> None:
        self._arm()

        def fail(_value: dict[str, object]) -> dict[str, str]:
            raise TimeoutError("snapshot deadline")

        result = task_guard.handle(
            self._payload(
                "UserPromptSubmit",
                prompt="Исправь findings последнего review EPIC-03a.",
            ),
            plugin_data=self.plugin_data,
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=self._loader("continue-implementation"),
            snapshot_loader=fail,
        )
        self.assertEqual("dls-task-guard-failed-open: TimeoutError", result["systemMessage"])
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

    def test_corrupt_binding_fails_open_once_and_is_removed(self) -> None:
        self._arm()
        binding = next(self.plugin_data.rglob("*.json"))
        binding.write_text("{}\n", encoding="utf-8")
        result = task_guard.handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="done"),
            plugin_data=self.plugin_data,
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=self._loader("continue-implementation"),
        )
        self.assertEqual("dls-task-guard-failed-open: RuntimeError", result["systemMessage"])
        self.assertEqual([], list(self.plugin_data.rglob("*.json")))

        quiet = task_guard.handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="done"),
            plugin_data=self.plugin_data,
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=self._loader("continue-implementation"),
        )
        self.assertIsNone(quiet)

    def test_legacy_binding_is_removed_fail_open(self) -> None:
        binding = task_guard._binding_path(self.plugin_data, self.session_id)
        binding.parent.mkdir(parents=True)
        binding.write_text(
            json.dumps({"contract": "dls-runtime-completion-guard/v2"}) + "\n",
            encoding="utf-8",
        )
        result = self._handle(
            self._payload("UserPromptSubmit", prompt="Какой статус EPIC-03a?"),
            "continue-implementation",
        )
        self.assertEqual("dls-task-guard-failed-open: RuntimeError", result["systemMessage"])
        self.assertFalse(binding.exists())

    def test_expired_binding_is_removed_without_reusing_consent(self) -> None:
        self._arm()
        binding = next(self.plugin_data.rglob("*.json"))
        value = json.loads(binding.read_text(encoding="utf-8"))
        value["created_at"] = time.time() - task_guard.BINDING_TTL_SECONDS - 1
        binding.write_text(json.dumps(value) + "\n", encoding="utf-8")
        result = self._handle(
            self._payload("Stop", stop_hook_active=False, last_assistant_message="done"),
            "continue-implementation",
        )
        self.assertIsNone(result)
        self.assertFalse(binding.exists())

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

    def test_hooks_json_bootstrap_delegates_to_present_guard(self) -> None:
        config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        environment = {
            **os.environ,
            "PLUGIN_ROOT": str(HOOK_PATH.parents[1]),
            "PLUGIN_DATA": str(self.plugin_data / "bootstrap-data"),
        }
        result = subprocess.run(
            command,
            shell=True,
            executable="/bin/sh",
            input=json.dumps(self._payload("UserPromptSubmit", prompt="Объясни код.")),
            text=True,
            capture_output=True,
            env=environment,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual({}, json.loads(result.stdout))

    def test_captured_bootstrap_fails_open_after_plugin_root_is_removed(self) -> None:
        config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        command = config["hooks"]["Stop"][0]["hooks"][0]["command"]
        missing = self.plugin_data / "removed-plugin-root"
        environment = {
            **os.environ,
            "PLUGIN_ROOT": str(missing),
            "PLUGIN_DATA": str(self.plugin_data / "bootstrap-data"),
        }
        result = subprocess.run(
            command,
            shell=True,
            executable="/bin/sh",
            input=json.dumps(self._payload("Stop", stop_hook_active=True)),
            text=True,
            capture_output=True,
            env=environment,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            "dls-hook-upgrade-required: restart Codex and open a fresh task.",
            payload["systemMessage"],
        )
        self.assertNotIn("decision", payload)
        self.assertNotIn("FileNotFoundError", result.stdout + result.stderr)
        self.assertEqual([], list((self.plugin_data / "bootstrap-data").rglob("*.json")))

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
                snapshot_loader=lambda _value: {
                    "common_dir_hash": "common",
                    "owner_gitdir_hash": "gitdir",
                    "head_hash": "head",
                    "draft_digest": "draft",
                },
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
        self.assertIn("continue-implementation", continued["reason"])
        (root / "src.py").write_text("draft = 2\n", encoding="utf-8")
        progressed = task_guard.handle(
            stop,
            plugin_data=guard_data,
            continue_actions=IMPLEMENTATION_CONTINUE_ACTIONS,
            status_loader=owner_status,
        )
        self.assertEqual("block", progressed["decision"])
        binding = json.loads(next(guard_data.rglob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(2, binding["continuation_count"])


if __name__ == "__main__":
    unittest.main()
