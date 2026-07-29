from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dls_core.candidate_runner import candidate_ready
from dls_core.dependencies import (
    DEPENDENCY_EXCEPTION_CONTRACT,
    dependency_list,
    dependency_record_digest,
    dependency_set,
)
from dls_core.errors import IntegrityError
from dls_core.operations import approve, build_context, check
from dls_core.parallel_delivery import (
    change_readiness,
    delivery_map,
    overlap_projection,
)
from dls_core.io import utc_now
from dls_core.state import (
    StateStore,
    current_definition_digest,
    derived_approval_statuses,
)
from dls_core.worktrees import (
    resolve_change_root,
    worktree_create,
    worktree_register,
)

from support import (
    create_change,
    git,
    initialize,
    initialize_git,
    start_review_with_fake_codex,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CLI = PLUGIN_ROOT / "scripts/dls.py"


class ParallelDeliveryV090Tests(unittest.TestCase):
    def _repository(self, directory: str) -> tuple[Path, str]:
        root = Path(directory) / "repository"
        root.mkdir()
        initialize_git(root)
        (root / "shared.txt").write_text("baseline\n", encoding="utf-8")
        git(root, "add", "shared.txt")
        git(root, "commit", "-m", "shared baseline")
        return root, git(root, "rev-parse", "HEAD")

    def _worktree(
        self,
        root: Path,
        *,
        change_id: str,
        base: str,
        purpose: str = "implementation",
        control: str = "standard",
    ) -> Path:
        owner = root.parent / f"owner-{change_id}"
        created = worktree_create(
            root,
            change_id=change_id,
            base_ref=base,
            purpose=purpose,
            owner_path=owner,
            branch=f"codex/{change_id.lower()}-{purpose}",
        )
        self.assertTrue(created["changed"])
        initialize(owner)
        create_change(owner, change_id=change_id, control=control)
        git(owner, "add", ".dls", "docs")
        git(owner, "commit", "-m", f"initialize {change_id}")
        worktree_register(
            root,
            change_id=change_id,
            owner_path=owner.resolve(),
            base_ref=base,
            purpose=purpose,
        )
        return owner

    def _pair(self, directory: str) -> tuple[Path, Path, Path, str]:
        root, base = self._repository(directory)
        first = self._worktree(root, change_id="C001", base=base)
        second = self._worktree(root, change_id="C002", base=base)
        return root, first, second, base

    def _approve_definition(self, owner: Path, change_id: str) -> None:
        state = StateStore(owner).load(change_id)
        approve(
            owner,
            change_id=change_id,
            decision="definition",
            expected_revision=state["state_revision"],
            actor="user",
            prompt=None,
            response=None,
            git_sha=None,
            conditions=None,
            operation_id=f"approve-{change_id}-{state['state_revision']}",
        )

    def _configure_candidate(self, owner: Path, change_id: str) -> None:
        config = owner / ".dls/config.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                "[policy]\n",
                '[policy]\nreview_required_commands = ["canonical"]\n',
                1,
            )
            + f"""

[commands.canonical]
argv = ["{sys.executable}", "-c", "print('canonical pass')"]
cwd = "."
timeout_seconds = 5
max_output_bytes = 4096
env_allow = []
""",
            encoding="utf-8",
        )

    def _record_human_decision(
        self,
        owner: Path,
        change_id: str,
        *,
        decision: str,
        git_sha: str,
        conditions: str | None = None,
    ) -> None:
        store = StateStore(owner)
        state = store.load(change_id)
        approval = {
            "id": f"fixture-{decision}-{change_id}-{state['state_revision']}",
            "decision": decision,
            "object_digest": current_definition_digest(owner, state),
            "git_sha": git_sha,
            "actor": "user",
            "authority": "user",
            "recorded_at": utc_now(),
            "status": "current",
            "conditions": conditions,
            "prompt": None,
            "response": None,
        }

        def mutate(value: dict) -> None:
            value["approvals"].append(approval)
            if decision == "accept":
                value["phase"] = "accepted"
                value["lifecycle"] = "accepted"

        store.mutate(
            change_id,
            expected_revision=state["state_revision"],
            operation_id=approval["id"],
            operation_kind=f"fixture-{decision}",
            mutator=mutate,
        )

    def test_implementation_dependency_does_not_block_definition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, first, second, _ = self._pair(directory)
            dependency_set(
                second,
                change_id="C002",
                target_change_id="C001",
                blocks_stage="implementation",
                requires="accepted-in-base",
                rationale="Implementation consumes the accepted transport baseline.",
                operation_id="dependency-c002-c001",
            )
            definition = change_readiness(
                second, change_id="C002", stage="definition"
            )
            implementation = change_readiness(
                second, change_id="C002", stage="implementation"
            )
            self.assertTrue(definition["ready"])
            self.assertFalse(implementation["ready"])
            self.assertEqual(
                implementation["next_action"]["id"], "wait-dependency"
            )
            listed = dependency_list(second, change_id="C002")
            self.assertEqual(listed["dependencies"][0]["change_id"], "C001")
            self.assertEqual(first.name, "owner-C001")
            definition_gate = check(second, change_id="C002", gate="definition")
            dependency_check = next(
                item
                for item in definition_gate["checks"]
                if item["id"] == "dependencies:definition"
            )
            self.assertTrue(dependency_check["ok"])
            context = build_context(
                second,
                change_id="C002",
                phase="implementation",
                include=[],
                exclude=[],
                dry_run=True,
            )
            self.assertEqual(context["next_action"]["id"], "wait-dependency")
            blocked_candidate = candidate_ready(
                second,
                change_id="C002",
                base_ref=None,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(
                blocked_candidate["next_action"]["id"], "wait-dependency"
            )
            self.assertFalse(
                StateStore(second).load("C002").get("candidate_runs", [])
            )

    def test_registered_owner_wins_over_portable_stale_state_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, first, second, _ = self._pair(directory)
            stale_copy = second / ".dls/state/C001.json"
            shutil.copy2(first / ".dls/state/C001.json", stale_copy)
            self.assertEqual(resolve_change_root(second, "C001"), first.resolve())

    def test_dependency_contract_and_target_drift_stale_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, first, second, _ = self._pair(directory)
            self._approve_definition(second, "C002")
            dependency_set(
                second,
                change_id="C002",
                target_change_id="C001",
                blocks_stage="implementation",
                requires="definition-approved",
                rationale="Use the approved upstream contract.",
                operation_id="dependency-after-approval",
            )
            statuses = derived_approval_statuses(
                second, StateStore(second).load("C002")
            )
            self.assertEqual(statuses[-1]["status"], "stale")

            self._approve_definition(second, "C002")
            target_state = StateStore(first).load("C001")
            target_artifact = first / target_state["artifacts"]["spec"]["path"]
            target_artifact.write_text(
                target_artifact.read_text(encoding="utf-8")
                + "\nDefinition drift.\n",
                encoding="utf-8",
            )
            statuses = derived_approval_statuses(
                second, StateStore(second).load("C002")
            )
            self.assertEqual(statuses[-1]["status"], "stale")
            readiness = change_readiness(
                second, change_id="C002", stage="implementation"
            )
            self.assertEqual(
                readiness["dependencies"]["items"][0]["reason"],
                "target-definition-drift",
            )

    def test_cycle_and_excessive_depth_are_rejected_before_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            for index in range(18):
                create_change(root, change_id=f"C{index:03d}")
            for index in reversed(range(1, 17)):
                dependency_set(
                    root,
                    change_id=f"C{index:03d}",
                    target_change_id=f"C{index + 1:03d}",
                    blocks_stage="implementation",
                    requires="definition-approved",
                    rationale="Depth fixture.",
                    operation_id=f"depth-{index}",
                )
            with self.assertRaisesRegex(IntegrityError, "exceeds depth 16"):
                dependency_set(
                    root,
                    change_id="C000",
                    target_change_id="C001",
                    blocks_stage="implementation",
                    requires="definition-approved",
                    rationale="Too deep.",
                    operation_id="depth-16",
                )
            with self.assertRaisesRegex(IntegrityError, "cycle"):
                dependency_set(
                    root,
                    change_id="C017",
                    target_change_id="C001",
                    blocks_stage="implementation",
                    requires="definition-approved",
                    rationale="Cycle fixture.",
                    operation_id="cycle",
                )

    def test_accepted_in_base_requires_ancestry_or_exact_scoped_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, first, second, base = self._pair(directory)
            third = self._worktree(root, change_id="C003", base=base)
            for owner, change_id in ((second, "C002"), (third, "C003")):
                dependency_set(
                    owner,
                    change_id=change_id,
                    target_change_id="C001",
                    blocks_stage="implementation",
                    requires="accepted-in-base",
                    rationale="Consume the accepted C001 baseline.",
                    operation_id=f"dependency-{change_id}",
                )
            target_head = git(first, "rev-parse", "HEAD")
            self._record_human_decision(
                first,
                "C001",
                decision="accept",
                git_sha=target_head,
            )

            blocked = change_readiness(
                second, change_id="C002", stage="implementation"
            )
            self.assertEqual(
                blocked["next_action"]["id"], "rebase-after-dependency"
            )
            git(second, "merge", "--no-edit", "--no-ff", "-s", "ours", target_head)
            self.assertTrue(
                change_readiness(
                    second, change_id="C002", stage="implementation"
                )["ready"]
            )

            third_state = StateStore(third).load("C003")
            dependency = third_state["dependencies"][0]
            dependent_head = git(third, "rev-parse", "HEAD")
            conditions = json.dumps(
                {
                    "contract": DEPENDENCY_EXCEPTION_CONTRACT,
                    "dependency_digest": dependency_record_digest(dependency),
                    "target_head_sha": target_head,
                    "dependent_head_sha": dependent_head,
                },
                sort_keys=True,
            )
            self._record_human_decision(
                third,
                "C003",
                decision="exception",
                git_sha=dependent_head,
                conditions=conditions,
            )
            self.assertTrue(
                change_readiness(
                    third, change_id="C003", stage="implementation"
                )["ready"]
            )

    def test_worktree_create_is_dirty_root_safe_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, base = self._repository(directory)
            (root / "README.md").write_text("dirty main\n", encoding="utf-8")
            owner = root.parent / "definition-owner"
            first = worktree_create(
                root,
                change_id="C013",
                base_ref=base,
                purpose="definition",
                owner_path=owner,
                branch="codex/c013-definition",
            )
            second = worktree_create(
                root,
                change_id="C013",
                base_ref=base,
                purpose="definition",
                owner_path=owner,
                branch="codex/c013-definition",
            )
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])
            self.assertEqual(git(owner, "rev-parse", "HEAD"), base)
            self.assertEqual(
                git(root, "status", "--short", "README.md"), "M README.md"
            )
            with self.assertRaisesRegex(IntegrityError, "branch does not match"):
                worktree_create(
                    root,
                    change_id="C013",
                    base_ref=base,
                    purpose="definition",
                    owner_path=owner,
                    branch="codex/c013-other",
                )

    def test_exact_overlap_only_blocks_later_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, first, second, _ = self._pair(directory)
            for owner, marker in ((first, "first"), (second, "second")):
                (owner / "shared.txt").write_text(marker + "\n", encoding="utf-8")
                git(owner, "add", "shared.txt")
                git(owner, "commit", "-m", f"{marker} overlap")
            first_overlap = overlap_projection(first, change_id="C001")
            second_overlap = overlap_projection(second, change_id="C002")
            self.assertEqual(first_overlap["exact_overlap_count"], 1)
            self.assertFalse(first_overlap["blocked"])
            self.assertTrue(second_overlap["blocked"])
            self.assertEqual(
                second_overlap["next_action"]["id"],
                "wait-integration-predecessor",
            )

    def test_dependency_and_overlap_drift_invalidate_prepared_pack_before_model(self) -> None:
        for drift_kind in ("dependency", "overlap"):
            with self.subTest(drift_kind=drift_kind), tempfile.TemporaryDirectory() as directory:
                _, first, second, base = self._pair(directory)
                self._configure_candidate(second, "C002")
                self._approve_definition(second, "C002")
                (second / "shared.txt").write_text("second candidate\n", encoding="utf-8")
                git(second, "add", ".dls", "docs", "shared.txt")
                git(second, "commit", "-m", "prepare second candidate")
                prepared = candidate_ready(
                    second,
                    change_id="C002",
                    base_ref=base,
                    addressed=[],
                    noted=[],
                    extra_commands=[],
                    operation_id=None,
                )
                self.assertEqual(prepared["next_action"]["id"], "open-review-task")
                if drift_kind == "dependency":
                    dependency_set(
                        second,
                        change_id="C002",
                        target_change_id="C001",
                        blocks_stage="review",
                        requires="accepted-in-base",
                        rationale="Late dependency drift.",
                        operation_id="late-dependency",
                    )
                else:
                    (first / "shared.txt").write_text("first predecessor\n", encoding="utf-8")
                    git(first, "add", "shared.txt")
                    git(first, "commit", "-m", "create late overlap")
                before = len(StateStore(second).load("C002").get("native_reviews", []))
                with self.assertRaises(IntegrityError):
                    start_review_with_fake_codex(
                        second,
                        change_id="C002",
                        operation_id=f"blocked-{drift_kind}",
                    )
                after = len(StateStore(second).load("C002").get("native_reviews", []))
                self.assertEqual(after, before)

    def test_non_overlapping_changes_are_parallel_ready_and_map_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, first, second, _ = self._pair(directory)
            (first / "first.txt").write_text("first\n", encoding="utf-8")
            git(first, "add", "first.txt")
            git(first, "commit", "-m", "first change")
            (second / "second.txt").write_text("second\n", encoding="utf-8")
            git(second, "add", "second.txt")
            git(second, "commit", "-m", "second change")
            self.assertTrue(
                change_readiness(
                    first, change_id="C001", stage="review", include_overlap=True
                )["ready"]
            )
            self.assertTrue(
                change_readiness(
                    second, change_id="C002", stage="review", include_overlap=True
                )["ready"]
            )
            result = delivery_map(root)
            self.assertEqual(result["contract"], "dls-delivery-map/v1")
            self.assertEqual(result["parallel_groups"], [["C001", "C002"]])
            self.assertEqual(result["integration_order"], ["C001", "C002"])
            encoded = json.dumps(result)
            self.assertNotIn(str(root.parent), encoded)
            self.assertNotIn("owner_root", encoded)

    def test_two_changes_run_candidate_validation_without_global_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, first, second, base = self._pair(directory)
            for owner, change_id, filename in (
                (first, "C001", "first.txt"),
                (second, "C002", "second.txt"),
            ):
                self._configure_candidate(owner, change_id)
                self._approve_definition(owner, change_id)
                (owner / filename).write_text(change_id + "\n", encoding="utf-8")
                git(owner, "add", ".dls", "docs", filename)
                git(owner, "commit", "-m", f"candidate {change_id}")

            def prepare(owner: Path, change_id: str) -> dict:
                return candidate_ready(
                    owner,
                    change_id=change_id,
                    base_ref=base,
                    addressed=[],
                    noted=[],
                    extra_commands=[],
                    operation_id=None,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(prepare, first, "C001")
                second_future = pool.submit(prepare, second, "C002")
                results = [first_future.result(), second_future.result()]
            self.assertEqual(
                [item["next_action"]["id"] for item in results],
                ["open-review-task", "open-review-task"],
            )
            self.assertNotEqual(results[0]["run_id"], results[1]["run_id"])

    def test_two_routine_changes_run_candidate_and_review_without_global_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, base = self._repository(directory)
            first = self._worktree(
                root, change_id="C001", base=base, control="routine"
            )
            second = self._worktree(
                root, change_id="C002", base=base, control="routine"
            )
            for owner, change_id, filename in (
                (first, "C001", "first.txt"),
                (second, "C002", "second.txt"),
            ):
                self._configure_candidate(owner, change_id)
                self._approve_definition(owner, change_id)
                (owner / filename).write_text(change_id + "\n", encoding="utf-8")
                git(owner, "add", ".dls", "docs", filename)
                git(owner, "commit", "-m", f"routine candidate {change_id}")

            fake_bin = root.parent / "fake-bin"
            fake_bin.mkdir()
            executable = fake_bin / "codex"
            decision = json.dumps(
                {
                    "verdict": "review-clear",
                    "summary": "No findings.",
                    "findings": [],
                    "prior_finding_verdicts": [],
                },
                separators=(",", ":"),
            )
            executable.write_text(
                "#!/bin/sh\n"
                "output=''\n"
                "while [ \"$#\" -gt 0 ]; do\n"
                "  if [ \"$1\" = \"-o\" ] || [ \"$1\" = \"--output-last-message\" ]; then\n"
                "    output=\"$2\"; shift 2\n"
                "  else shift\n"
                "  fi\n"
                "done\n"
                "[ -n \"$output\" ] || exit 65\n"
                f"printf '%s' {json.dumps(decision)} > \"$output\"\n"
                "printf '%s\\n' '{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":10,\"cached_input_tokens\":0,\"output_tokens\":1,\"reasoning_output_tokens\":0}}'\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)
            original_path = os.environ.get("PATH")
            os.environ["PATH"] = f"{fake_bin}{os.pathsep}{original_path or ''}"
            try:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [
                        pool.submit(
                            candidate_ready,
                            owner,
                            change_id=change_id,
                            base_ref=base,
                            addressed=[],
                            noted=[],
                            extra_commands=[],
                            operation_id=None,
                        )
                        for owner, change_id in ((first, "C001"), (second, "C002"))
                    ]
                    results = [future.result() for future in futures]
            finally:
                if original_path is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = original_path

            self.assertEqual(
                [item["status"] for item in results], ["completed", "completed"]
            )
            self.assertEqual(
                [item["verdict"] for item in results],
                ["review-clear", "review-clear"],
            )
            self.assertNotEqual(results[0]["review_id"], results[1]["review_id"])

    def test_public_cli_and_skill_keep_parallelism_explicit_and_non_autonomous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, base = self._repository(directory)
            initialize(root)
            create_change(root, change_id="C001")
            create_change(root, change_id="C002")
            command = [
                sys.executable,
                str(CLI),
                "--root",
                str(root),
                "--json",
                "dependency",
                "set",
                "C002",
                "--on",
                "C001",
                "--blocks",
                "implementation",
                "--requires",
                "accepted-in-base",
                "--rationale",
                "CLI fixture.",
                "--dry-run",
            ]
            dependency = json.loads(
                subprocess.run(
                    command,
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout
            )
            self.assertEqual(dependency["dependency"]["change_id"], "C001")
            preview = json.loads(
                subprocess.run(
                    [
                        sys.executable,
                        str(CLI),
                        "--root",
                        str(root),
                        "--json",
                        "worktree",
                        "create",
                        "C013",
                        "--base",
                        base,
                        "--purpose",
                        "definition",
                        "--dry-run",
                    ],
                    check=True,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ).stdout
            )
            self.assertEqual(preview["base_sha"], base)
            self.assertFalse(preview["changed"])

        skill = (PLUGIN_ROOT / "skills/dls-workflow/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("stage-aware dependencies", skill)
        self.assertIn("One writer means one owner worktree", skill)
        self.assertIn("The user still opens each parallel Codex task", skill)
        self.assertNotIn("create a subagent for parallel", skill.lower())


if __name__ == "__main__":
    unittest.main()
