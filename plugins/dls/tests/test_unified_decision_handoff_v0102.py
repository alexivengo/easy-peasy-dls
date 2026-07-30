from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dls_core.candidate_runner import candidate_ready, candidate_status
from dls_core.decisions import architecture_digest, design_digest
from dls_core.errors import IntegrityError
from dls_core.operations import approve, design_set, status
from dls_core.review_runner import review_run, review_status
from dls_core.state import StateStore, current_definition_digest
from dls_core.telemetry import delivery_status

from support import create_change, git, initialize, initialize_git


class UnifiedDecisionHandoffV0102Tests(unittest.TestCase):
    def _architecture_fixture(self, root: Path, *, command: bool = False) -> str:
        base = initialize_git(root)
        initialize(root)
        create_change(
            root,
            control="critical",
            impacts=["architecture"],
        )
        if command:
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "[policy]\n",
                    '[policy]\nreview_required_commands = ["test"]\n',
                    1,
                )
                + f'''\n[commands.test]\nargv = ["{sys.executable}", "-c", "print('pass')"]\ncwd = "."\ntimeout_seconds = 5\nmax_output_bytes = 4096\nenv_allow = []\n''',
                encoding="utf-8",
            )
        git(root, "add", ".dls", "docs")
        git(root, "commit", "-m", "architecture definition")
        return base

    def _record_legacy_definition(self, root: Path) -> None:
        store = StateStore(root)
        state = store.load("C001")
        digest = current_definition_digest(root, state)
        head = git(root, "rev-parse", "HEAD")

        def mutate(value: dict) -> None:
            value["approvals"].append(
                {
                    "id": "legacy-definition",
                    "decision": "definition",
                    "object_digest": digest,
                    "git_sha": head,
                    "actor": "user",
                    "authority": "user",
                    "recorded_at": "2026-01-01T00:00:00Z",
                    "status": "current",
                    "conditions": None,
                    "prompt": None,
                    "response": None,
                }
            )

        store.mutate(
            "C001",
            expected_revision=state["state_revision"],
            operation_id="legacy-definition",
            operation_kind="fixture:legacy-definition",
            mutator=mutate,
        )

    def _stale_legacy_definition(self, root: Path) -> dict:
        self._record_legacy_definition(root)
        store = StateStore(root)
        spec = root / store.load("C001")["artifacts"]["spec"]["path"]
        spec.write_text(
            spec.read_text(encoding="utf-8") + "\nUnrelated acceptance wording.\n",
            encoding="utf-8",
        )
        git(root, "add", "docs")
        git(root, "commit", "-m", "refine definition without architecture drift")
        return status(root, change_id="C001")

    def test_legacy_projection_requires_one_combined_final_approval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._architecture_fixture(root)
            readiness = self._stale_legacy_definition(root)
            action = readiness["next_action"]
            self.assertEqual(action["id"], "approve-definition-and-architecture")
            self.assertEqual(
                [item["decision"] for item in action["approvals"]],
                ["definition", "architecture"],
            )
            self.assertEqual(
                readiness["decisions"]["architecture"]["approval_provenance"],
                "legacy-definition-projection",
            )
            store = StateStore(root)
            revision = store.load("C001")["state_revision"]
            with self.assertRaises(IntegrityError):
                approve(
                    root,
                    change_id="C001",
                    decision="definition",
                    expected_revision=revision,
                    actor="user",
                    prompt=None,
                    response=None,
                    git_sha=None,
                    conditions=None,
                    operation_id="definition-only",
                )
            self.assertEqual(store.load("C001")["state_revision"], revision)
            projected = approve(
                root,
                change_id="C001",
                decision="definition",
                include_architecture=True,
                expected_revision=revision,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="combined-dry-run",
                dry_run=True,
            )
            self.assertTrue(projected["dry_run"])
            self.assertEqual(
                [item["decision"] for item in projected["approval_bundle"]["decisions"]],
                ["definition", "architecture"],
            )
            self.assertEqual(store.load("C001")["state_revision"], revision)
            result = approve(
                root,
                change_id="C001",
                decision="definition",
                include_architecture=True,
                expected_revision=revision,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="combined-final",
            )
            self.assertEqual(
                {item["decision"] for item in result["approvals"]},
                {"definition", "architecture"},
            )
            bundle_ids = {item["approval_bundle_id"] for item in result["approvals"]}
            self.assertEqual(bundle_ids, {result["approval_bundle"]["id"]})
            self.assertTrue(
                all(
                    item["approval_bundle_contract"] == "dls-approval-bundle/v1"
                    for item in result["approvals"]
                )
            )
            current = status(root, change_id="C001")
            self.assertIsNone(current["decision_next_action"])
            self.assertEqual(
                current["decisions"]["architecture"]["approval_provenance"],
                "scoped",
            )

    def test_codex_bundle_requires_every_decision_and_short_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._architecture_fixture(root)
            state = StateStore(root).load("C001")
            definition = current_definition_digest(root, state)
            architecture = architecture_digest(root, state)
            assert architecture is not None
            prompt = (
                f"Confirm definition {definition[:8]} and architecture "
                f"{architecture[:8]}."
            )
            revision = state["state_revision"]
            with self.assertRaises(IntegrityError):
                approve(
                    root,
                    change_id="C001",
                    decision="definition",
                    include_architecture=True,
                    expected_revision=revision,
                    actor="codex",
                    prompt=prompt,
                    response=f"Yes, definition {definition[:8]} is approved.",
                    git_sha=None,
                    conditions=None,
                    operation_id="missing-architecture-response",
                )
            self.assertEqual(StateStore(root).load("C001")["state_revision"], revision)
            result = approve(
                root,
                change_id="C001",
                decision="definition",
                include_architecture=True,
                expected_revision=revision,
                actor="codex",
                prompt=prompt,
                response=(
                    f"Yes, definition {definition[:8]} and architecture "
                    f"{architecture[:8]} are approved."
                ),
                git_sha=None,
                conditions=None,
                operation_id="complete-bundle-response",
            )
            self.assertTrue(result["changed"])

    def test_definition_design_architecture_bundle_is_atomic_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(
                root,
                control="critical",
                impacts=["architecture", "user-interface"],
            )
            design = root / "docs/design/C001.md"
            design.parent.mkdir(parents=True, exist_ok=True)
            design.write_text("# Approved surface\n", encoding="utf-8")
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "definition and design")
            design_set(
                root,
                change_id="C001",
                tier=2,
                surfaces=["settings"],
                source_kind="artifact",
                source_ref="docs/design/C001.md",
                source_version=None,
                bypass=False,
                rationale=None,
                risk=None,
                operation_id="design-source",
            )
            revision = StateStore(root).load("C001")["state_revision"]

            def invoke() -> dict:
                return approve(
                    root,
                    change_id="C001",
                    decision="definition",
                    include_design=True,
                    include_architecture=True,
                    expected_revision=revision,
                    actor="user",
                    prompt=None,
                    response=None,
                    git_sha=None,
                    conditions=None,
                    operation_id="triple-bundle",
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: invoke(), range(2)))
            self.assertEqual(sum(bool(item["changed"]) for item in results), 1)
            state = StateStore(root).load("C001")
            current = [item for item in state["approvals"] if item["status"] == "current"]
            self.assertEqual(
                {item["decision"] for item in current},
                {"definition", "design", "architecture"},
            )
            self.assertEqual(len({item["approval_bundle_id"] for item in current}), 1)
            self.assertIsNotNone(design_digest(root, state))

    def test_pending_decision_review_preflight_does_not_create_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._architecture_fixture(root)
            expected = self._stale_legacy_definition(root)["next_action"]
            before = StateStore(root).load("C001")
            result = review_run(
                root,
                change_id="C001",
                pack_path=None,
                operation_id="review-with-pending-decision",
            )
            after = StateStore(root).load("C001")
            self.assertEqual(result["status"], "not-prepared")
            self.assertEqual(result["next_action"], expected)
            self.assertIsNone(result["review_id"])
            self.assertIsNone(result["review_result_path"])
            self.assertEqual(after["state_revision"], before["state_revision"])
            self.assertEqual(after["reviews"], before["reviews"])
            self.assertEqual(after.get("candidate_runs", []), before.get("candidate_runs", []))

    def test_all_status_surfaces_share_the_same_decision_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._architecture_fixture(root)
            expected = self._stale_legacy_definition(root)["next_action"]
            actions = [
                candidate_status(root, change_id="C001")["next_action"],
                review_status(root, change_id="C001")["next_action"],
                delivery_status(root, change_id="C001")["next_action"],
            ]
            self.assertTrue(all(item == expected for item in actions))

    def test_partial_v0101_state_recovers_with_standalone_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._architecture_fixture(root)
            store = StateStore(root)
            state = store.load("C001")
            definition = current_definition_digest(root, state)
            head = git(root, "rev-parse", "HEAD")

            def record_partial(value: dict) -> None:
                value["approvals"].append(
                    {
                        "id": "v0101-definition",
                        "decision": "definition",
                        "object_digest": definition,
                        "git_sha": head,
                        "actor": "user",
                        "authority": "user",
                        "recorded_at": "2026-07-30T00:00:00Z",
                        "status": "current",
                        "conditions": None,
                        "prompt": None,
                        "response": None,
                        "definition_digest_contract": "dls-definition-digest/v2",
                        "decision_snapshots_contract": "dls-definition-decisions/v1",
                        "design_decision_digest": None,
                        "architecture_decision_digest": architecture_digest(root, state),
                    }
                )
                value["phase"] = "implementation"
                value["lifecycle"] = "approved"

            store.mutate(
                "C001",
                expected_revision=state["state_revision"],
                operation_id="partial-v0101",
                operation_kind="fixture:partial-v0101",
                mutator=record_partial,
            )
            pending = status(root, change_id="C001")
            self.assertEqual(pending["next_action"]["id"], "approve-architecture")
            recovered = approve(
                root,
                change_id="C001",
                decision="architecture",
                expected_revision=store.load("C001")["state_revision"],
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="recover-architecture",
            )
            self.assertEqual(recovered["next_action"]["id"], "continue-implementation")
            self.assertIsNone(status(root, change_id="C001")["decision_next_action"])

    def test_atomic_approval_can_prepare_exact_head_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._architecture_fixture(root, command=True)
            revision = StateStore(root).load("C001")["state_revision"]
            approved = approve(
                root,
                change_id="C001",
                decision="definition",
                include_architecture=True,
                expected_revision=revision,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="approve-bundle",
            )
            self.assertEqual(approved["next_action"]["id"], "continue-implementation")
            (root / "README.md").write_text("# Implemented candidate\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "implement candidate")
            self.assertEqual(
                candidate_status(root, change_id="C001")["next_action"]["id"],
                "run-candidate-ready",
            )
            candidate = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id="candidate-after-bundle",
            )
            self.assertEqual(candidate["status"], "completed")
            self.assertEqual(candidate["next_action"]["id"], "open-review-task")
            pack = json.loads(
                (root / candidate["review_pack_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(pack["head_sha"], git(root, "rev-parse", "HEAD"))
            self.assertEqual(
                pack["decisions"]["architecture"]["approval_provenance"],
                "scoped",
            )

    def test_bundle_rejects_wrong_head_and_dirty_architecture_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._architecture_fixture(root)
            store = StateStore(root)
            revision = store.load("C001")["state_revision"]
            with self.assertRaises(IntegrityError):
                approve(
                    root,
                    change_id="C001",
                    decision="definition",
                    include_architecture=True,
                    expected_revision=revision,
                    actor="user",
                    prompt=None,
                    response=None,
                    git_sha="0" * 40,
                    conditions=None,
                    operation_id="wrong-head",
                )
            self.assertEqual(store.load("C001")["state_revision"], revision)
            state = store.load("C001")
            spec = root / state["artifacts"]["spec"]["path"]
            spec.write_text(
                spec.read_text(encoding="utf-8") + "\nDirty decision input.\n",
                encoding="utf-8",
            )
            with self.assertRaises(IntegrityError):
                approve(
                    root,
                    change_id="C001",
                    decision="definition",
                    include_architecture=True,
                    expected_revision=revision,
                    actor="user",
                    prompt=None,
                    response=None,
                    git_sha=None,
                    conditions=None,
                    operation_id="dirty-architecture",
                )
            self.assertEqual(store.load("C001")["state_revision"], revision)
            self.assertFalse(store.load("C001")["approvals"])

    def test_cli_and_skill_expose_one_explicit_combined_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._architecture_fixture(root)
            state = StateStore(root).load("C001")
            cli = Path(__file__).resolve().parents[1] / "scripts/dls.py"
            result = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "--root",
                    str(root),
                    "--json",
                    "approve",
                    "C001",
                    "--decision",
                    "definition",
                    "--include-architecture",
                    "--actor",
                    "user",
                    "--expect-revision",
                    str(state["state_revision"]),
                    "--operation-id",
                    "cli-combined",
                ],
                cwd=root,
                env=dict(os.environ),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(
                {item["decision"] for item in payload["approvals"]},
                {"definition", "architecture"},
            )
        skill_root = Path(__file__).resolve().parents[1] / "skills/dls-workflow"
        skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        gates = (skill_root / "references/gates.md").read_text(encoding="utf-8")
        review = (skill_root / "references/review.md").read_text(encoding="utf-8")
        combined = skill + gates + review
        self.assertIn("approve-definition-and-design-and-architecture", combined)
        self.assertIn("Never infer architecture", combined)
        self.assertIn("invoke `candidate-ready` only for `run-candidate-ready`", skill)
        self.assertIn("Never\n  record approval or run `candidate-ready`", review)
        self.assertNotIn("approval subagent", combined.lower())


if __name__ == "__main__":
    unittest.main()
