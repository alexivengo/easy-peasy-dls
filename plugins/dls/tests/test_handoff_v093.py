from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dls_core.candidate_runner import candidate_ready, candidate_status
from dls_core.cli import build_parser, dispatch
from dls_core.dependencies import dependency_set
from dls_core.errors import IntegrityError
from dls_core.io import utc_now
from dls_core.operations import approve, build_context
from dls_core.repo import package_digest
from dls_core.state import (
    StateStore,
    current_definition_digest,
    derived_approval_statuses,
)
from dls_core.worktrees import (
    worktree_create,
    worktree_prepare,
    worktree_register,
)

from support import create_change, git, initialize, initialize_git


class HandoffV093Tests(unittest.TestCase):
    def _configure_candidate(self, root: Path) -> None:
        config = root / ".dls/config.toml"
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

    def _approve(self, root: Path, change_id: str) -> dict:
        state = StateStore(root).load(change_id)
        return approve(
            root,
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

    def _register_parallel_change(self, root: Path, base: str) -> Path:
        owner = root.parent / "owner-C001"
        worktree_create(
            root,
            change_id="C001",
            base_ref=base,
            purpose="implementation",
            owner_path=owner,
            branch="codex/c001-implementation",
        )
        initialize(owner)
        create_change(owner, change_id="C001", control="routine")
        git(owner, "add", ".dls", "docs")
        git(owner, "commit", "-m", "initialize parallel change")
        self._approve(owner, "C001")
        worktree_register(
            root,
            change_id="C001",
            owner_path=owner,
            base_ref=base,
            purpose="implementation",
        )
        return owner

    def test_definition_approval_requires_committed_authored_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, change_id="C002", control="critical", tickets=True)
            state = StateStore(root).load("C002")
            with self.assertRaisesRegex(
                IntegrityError,
                "requires committed authored artifacts",
            ):
                approve(
                    root,
                    change_id="C002",
                    decision="definition",
                    expected_revision=state["state_revision"],
                    actor="user",
                    prompt=None,
                    response=None,
                    git_sha=None,
                    conditions=None,
                    operation_id="dirty-definition",
                )
            self.assertFalse(StateStore(root).load("C002")["approvals"])

            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "commit definition")
            approved = self._approve(root, "C002")
            self.assertEqual(approved["approval"]["git_sha"], git(root, "rev-parse", "HEAD"))
            self.assertEqual(
                approved["approval"]["definition_digest_contract"],
                "dls-definition-digest/v2",
            )

    def test_atomic_owner_handoff_preserves_approval_and_ignores_execution_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            root.mkdir()
            initial_base = initialize_git(root)
            self._register_parallel_change(root, initial_base)
            initialize(root)
            create_change(root, change_id="C002", control="critical", tickets=True)
            self._configure_candidate(root)

            store = StateStore(root)
            state = store.load("C002")
            changelog = root / "docs/C002-change/CHANGELOG-C002.md"
            changelog.parent.mkdir(parents=True, exist_ok=True)
            changelog.write_text("# Changelog\n\nDefinition prepared.\n", encoding="utf-8")

            def prepare_definition(value: dict) -> None:
                value["artifacts"]["changelog"] = {
                    "path": "docs/C002-change/CHANGELOG-C002.md",
                    "role": "execution",
                }
                for ticket in value["tickets"].values():
                    ticket["status"] = "done"

            store.mutate(
                "C002",
                expected_revision=state["state_revision"],
                operation_id="prepare-definition-fixture",
                operation_kind="fixture",
                mutator=prepare_definition,
            )
            dependency_set(
                root,
                change_id="C002",
                target_change_id="C001",
                blocks_stage="acceptance",
                requires="definition-approved",
                rationale="Acceptance records the parallel prerequisite.",
                operation_id="dependency-c002-c001",
            )
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "approved definition")
            definition_head = git(root, "rev-parse", "HEAD")
            approved = self._approve(root, "C002")
            approved_digest = approved["approval"]["object_digest"]

            before_owner = candidate_status(root, change_id="C002")
            self.assertEqual(
                before_owner["next_action"]["id"],
                "prepare-owner-worktree",
            )
            blocked_candidate = candidate_ready(
                root,
                change_id="C002",
                base_ref=definition_head,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(
                blocked_candidate["next_action"]["id"],
                "prepare-owner-worktree",
            )
            blocked_context = build_context(
                root,
                change_id="C002",
                phase="implementation",
                include=[],
                exclude=[],
            )
            self.assertEqual(
                blocked_context["next_action"]["id"],
                "prepare-owner-worktree",
            )

            owner = root.parent / "owner-C002"
            arguments = build_parser().parse_args(
                [
                    "worktree",
                    "prepare",
                    "C002",
                    "--base",
                    definition_head,
                    "--path",
                    str(owner),
                    "--branch",
                    "codex/c002-implementation",
                    "--dry-run",
                ]
            )
            projected = dispatch(root, arguments)
            self.assertEqual(projected["status"], "projected")
            self.assertFalse(owner.exists())
            prepared = worktree_prepare(
                root,
                change_id="C002",
                base_ref=definition_head,
                purpose="implementation",
                owner_path=owner,
                branch="codex/c002-implementation",
            )
            self.assertTrue(prepared["changed"])
            self.assertEqual(prepared["next_action"]["id"], "continue-implementation")
            transferred = StateStore(owner).load("C002")
            self.assertEqual(current_definition_digest(owner, transferred), approved_digest)
            self.assertEqual(
                transferred["dependencies"],
                StateStore(root).load("C002")["dependencies"],
            )
            self.assertEqual(len(transferred["dependencies"]), 1)
            self.assertTrue(
                any(
                    item.get("decision") == "definition"
                    and item.get("status") == "current"
                    for item in derived_approval_statuses(owner, transferred)
                )
            )

            routed = candidate_status(root, change_id="C002")
            self.assertEqual(Path(routed["owner_root"]).resolve(), owner.resolve())
            self.assertEqual(routed["next_action"]["id"], "continue-implementation")

            (owner / "implementation.txt").write_text("implemented\n", encoding="utf-8")
            owner_changelog = owner / "docs/C002-change/CHANGELOG-C002.md"
            owner_changelog.write_text(
                owner_changelog.read_text(encoding="utf-8")
                + "\nValidation evidence: canonical PASS.\n",
                encoding="utf-8",
            )
            git(owner, "add", "implementation.txt", "docs/C002-change/CHANGELOG-C002.md")
            git(owner, "commit", "-m", "implement candidate and record evidence")
            candidate_head = git(owner, "rev-parse", "HEAD")
            self.assertEqual(
                current_definition_digest(owner, StateStore(owner).load("C002")),
                approved_digest,
            )
            ready_status = candidate_status(root, change_id="C002")
            self.assertEqual(ready_status["next_action"]["id"], "run-candidate-ready")

            candidate = candidate_ready(
                root,
                change_id="C002",
                base_ref=definition_head,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(candidate["next_action"]["id"], "open-review-task")
            self.assertEqual(candidate["candidate_head"], candidate_head)
            self.assertEqual(Path(candidate["owner_root"]).resolve(), owner.resolve())
            self.assertTrue((owner / candidate["review_pack_path"]).is_file())

            repeated = worktree_prepare(
                root,
                change_id="C002",
                base_ref=definition_head,
                purpose="implementation",
                owner_path=owner,
                branch="codex/c002-implementation",
            )
            self.assertFalse(repeated["changed"])

    def test_owner_handoff_refuses_to_overwrite_newer_existing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            root.mkdir()
            initialize_git(root)
            initialize(root)
            create_change(root, change_id="C002", control="critical")
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "definition")
            definition_head = git(root, "rev-parse", "HEAD")
            self._approve(root, "C002")

            owner = root.parent / "existing-C002"
            worktree_create(
                root,
                change_id="C002",
                base_ref=definition_head,
                purpose="implementation",
                owner_path=owner,
                branch="codex/existing-c002-implementation",
            )
            owner_store = StateStore(owner)
            owner_state = owner_store.load("C002")

            def local_progress(value: dict) -> None:
                value["lifecycle"] = "implemented"

            owner_store.mutate(
                "C002",
                expected_revision=owner_state["state_revision"],
                operation_id="local-unregistered-progress",
                operation_kind="fixture",
                mutator=local_progress,
            )
            with self.assertRaisesRegex(
                IntegrityError,
                "newer local DLS state",
            ):
                worktree_prepare(
                    root,
                    change_id="C002",
                    base_ref=definition_head,
                    purpose="implementation",
                    owner_path=owner,
                    branch="codex/existing-c002-implementation",
                )

    def test_existing_worktree_transfer_rolls_back_when_registration_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repository"
            root.mkdir()
            initialize_git(root)
            initialize(root)
            create_change(root, change_id="C002", control="standard")
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "definition")
            definition_head = git(root, "rev-parse", "HEAD")
            self._approve(root, "C002")

            owner = root.parent / "existing-C002"
            worktree_create(
                root,
                change_id="C002",
                base_ref=definition_head,
                purpose="implementation",
                owner_path=owner,
                branch="codex/rollback-c002-implementation",
            )
            owner_state_path = StateStore(owner).path("C002")
            before = owner_state_path.read_text(encoding="utf-8")
            with mock.patch(
                "dls_core.worktrees._register_prepared_owner",
                side_effect=IntegrityError("synthetic registry failure"),
            ):
                with self.assertRaisesRegex(IntegrityError, "synthetic registry failure"):
                    worktree_prepare(
                        root,
                        change_id="C002",
                        base_ref=definition_head,
                        purpose="implementation",
                        owner_path=owner,
                        branch="codex/rollback-c002-implementation",
                    )
            self.assertEqual(owner_state_path.read_text(encoding="utf-8"), before)

    def test_reproducible_legacy_approval_survives_changelog_only_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, change_id="C002", control="standard")
            changelog = root / "docs/changes/C002-c002-change/CHANGELOG-C002.md"
            changelog.write_text("# Changelog\n\nBefore.\n", encoding="utf-8")
            store = StateStore(root)
            state = store.load("C002")

            def make_legacy(value: dict) -> None:
                value.pop("definition_digest_contract", None)
                for metadata in value["artifacts"].values():
                    metadata.pop("role", None)
                value["artifacts"]["changelog"] = {
                    "path": "docs/changes/C002-c002-change/CHANGELOG-C002.md"
                }

            store.mutate(
                "C002",
                expected_revision=state["state_revision"],
                operation_id="legacy-artifacts",
                operation_kind="fixture",
                mutator=make_legacy,
            )
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "legacy approved package")
            approved_head = git(root, "rev-parse", "HEAD")
            legacy_state = store.load("C002")
            legacy_digest = package_digest(root, legacy_state["artifacts"])

            def add_legacy_approval(value: dict) -> None:
                value["approvals"].append(
                    {
                        "id": "legacy-definition",
                        "decision": "definition",
                        "object_digest": legacy_digest,
                        "git_sha": approved_head,
                        "actor": "user",
                        "authority": "user",
                        "recorded_at": utc_now(),
                        "status": "current",
                        "conditions": None,
                        "prompt": None,
                        "response": None,
                    }
                )
                value["phase"] = "implementation"
                value["lifecycle"] = "approved"

            store.mutate(
                "C002",
                expected_revision=legacy_state["state_revision"],
                operation_id="legacy-approval",
                operation_kind="fixture",
                mutator=add_legacy_approval,
            )
            changelog.write_text("# Changelog\n\nAfter evidence.\n", encoding="utf-8")
            git(root, "add", "docs/changes/C002-c002-change/CHANGELOG-C002.md")
            git(root, "commit", "-m", "record execution evidence")

            projected = derived_approval_statuses(root, store.load("C002"))[-1]
            self.assertEqual(projected["status"], "current")
            self.assertEqual(projected["recorded_object_digest"], legacy_digest)
            self.assertEqual(
                projected["object_digest"],
                current_definition_digest(root, store.load("C002")),
            )


if __name__ == "__main__":
    unittest.main()
