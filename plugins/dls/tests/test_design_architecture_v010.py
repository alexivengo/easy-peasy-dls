from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dls_core.candidate_runner import candidate_ready, candidate_status
from dls_core.decisions import (
    ARCHITECTURE_DIGEST_CONTRACT,
    DESIGN_DIGEST_CONTRACT,
    architecture_digest,
    design_digest,
)
from dls_core.delivery_receipt import delivery_receipt
from dls_core.errors import IntegrityError, UsageError
from dls_core.operations import (
    adopt_change,
    approve,
    build_context,
    check,
    design_set,
    design_status,
    status,
)
from dls_core.review_runner import review_status
from dls_core.state import StateStore, current_definition_digest
from dls_core.telemetry import delivery_status, review_metrics

from support import create_change, git, initialize, initialize_git


class DesignArchitectureV010Tests(unittest.TestCase):
    def _commit_ui_definition(
        self,
        root: Path,
        *,
        control: str = "standard",
        tier: int = 2,
        source_kind: str = "artifact",
    ) -> str:
        base = initialize_git(root)
        initialize(root)
        create_change(root, control=control, impacts=["user-interface"])
        design = root / "docs/design/C001-settings.md"
        design.parent.mkdir(parents=True, exist_ok=True)
        design.write_text("# Settings surface\n\nReuse the established row hierarchy.\n")
        git(root, "add", ".dls", "docs")
        git(root, "commit", "-m", "UI definition")
        design_set(
            root,
            change_id="C001",
            tier=tier,
            surfaces=["settings"],
            source_kind=source_kind,
            source_ref="docs/design/C001-settings.md",
            source_version=None,
            bypass=False,
            rationale=None,
            risk=None,
            operation_id="design-source",
        )
        return base

    def test_tier1_precedent_and_tier3_bypass_are_typed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._commit_ui_definition(root, control="routine", tier=1, source_kind="precedent")
            source = design_status(root, change_id="C001")["design"]
            self.assertEqual(source["tier"], 1)
            self.assertEqual(source["source_kind"], "precedent")
            self.assertFalse(source["bypass"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="routine", impacts=["user-interface"])
            bypass = design_set(
                root,
                change_id="C001",
                tier=3,
                surfaces=["authentication"],
                source_kind=None,
                source_ref=None,
                source_version=None,
                bypass=True,
                rationale="Ship the bounded native authentication surface without a mockup.",
                risk="high",
                operation_id="bypass",
            )
            self.assertTrue(bypass["design"]["bypass"])
            with self.assertRaises(UsageError):
                design_set(
                    root,
                    change_id="C001",
                    tier=3,
                    surfaces=["authentication"],
                    source_kind=None,
                    source_ref=None,
                    source_version=None,
                    bypass=True,
                    rationale="",
                    risk=None,
                    operation_id="invalid-bypass",
                )

    def test_external_source_requires_https_and_immutable_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="routine", impacts=["user-interface"])
            with self.assertRaises(IntegrityError):
                design_set(
                    root,
                    change_id="C001",
                    tier=2,
                    surfaces=["settings"],
                    source_kind="external-version",
                    source_ref="http://example.invalid/file",
                    source_version="v1",
                    bypass=False,
                    rationale=None,
                    risk=None,
                    operation_id="http",
                )
            with self.assertRaises(UsageError):
                design_set(
                    root,
                    change_id="C001",
                    tier=2,
                    surfaces=["settings"],
                    source_kind="external-version",
                    source_ref="https://example.invalid/file",
                    source_version=None,
                    bypass=False,
                    rationale=None,
                    risk=None,
                    operation_id="unversioned",
                )
            result = design_set(
                root,
                change_id="C001",
                tier=2,
                surfaces=["settings"],
                source_kind="external-version",
                source_ref="https://example.invalid/file",
                source_version="immutable:42",
                bypass=False,
                rationale=None,
                risk=None,
                operation_id="external",
            )
            self.assertEqual(result["design"]["source_kind"], "external-version")
            self.assertNotIn("example.invalid", json.dumps(result))
            with self.assertRaises(IntegrityError):
                design_set(
                    root,
                    change_id="C001",
                    tier=2,
                    surfaces=["settings"],
                    source_kind="external-version",
                    source_ref="https://user:secret@example.invalid/file",
                    source_version="immutable:42",
                    bypass=False,
                    rationale=None,
                    risk=None,
                    operation_id="credentialed",
                )

    def test_tier3_rejects_precedent_without_explicit_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="critical", impacts=["user-interface"])
            precedent = root / "docs/design/existing-screen.md"
            precedent.parent.mkdir(parents=True, exist_ok=True)
            precedent.write_text("# Existing screen\n")
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "definition and precedent")
            with self.assertRaises(UsageError):
                design_set(
                    root,
                    change_id="C001",
                    tier=3,
                    surfaces=["authentication"],
                    source_kind="precedent",
                    source_ref="docs/design/existing-screen.md",
                    source_version=None,
                    bypass=False,
                    rationale=None,
                    risk=None,
                    operation_id="tier3-precedent",
                )

    def test_repository_source_rejects_untracked_traversal_and_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="routine", impacts=["user-interface"])
            untracked = root / "design.md"
            untracked.write_text("untracked\n")
            with self.assertRaises(IntegrityError):
                design_set(
                    root,
                    change_id="C001",
                    tier=2,
                    surfaces=["settings"],
                    source_kind="artifact",
                    source_ref="design.md",
                    source_version=None,
                    bypass=False,
                    rationale=None,
                    risk=None,
                    operation_id="untracked",
                )
            with self.assertRaises(IntegrityError):
                design_set(
                    root,
                    change_id="C001",
                    tier=2,
                    surfaces=["settings"],
                    source_kind="artifact",
                    source_ref="../design.md",
                    source_version=None,
                    bypass=False,
                    rationale=None,
                    risk=None,
                    operation_id="traversal",
                )
            git(root, "add", "design.md")
            git(root, "commit", "-m", "design")
            design_set(
                root,
                change_id="C001",
                tier=2,
                surfaces=["settings"],
                source_kind="artifact",
                source_ref="design.md",
                source_version=None,
                bypass=False,
                rationale=None,
                risk=None,
                operation_id="tracked",
            )
            untracked.write_text("changed\n")
            git(root, "add", "design.md")
            git(root, "commit", "-m", "design drift")
            result = design_status(root, change_id="C001")
            self.assertFalse(result["design"]["provenance_current"])
            self.assertEqual(result["next_action"]["id"], "record-design-source")

    def test_scoped_design_approval_survives_unrelated_spec_but_not_design_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._commit_ui_definition(root)
            revision = StateStore(root).load("C001")["state_revision"]
            approved = approve(
                root,
                change_id="C001",
                decision="definition",
                include_design=True,
                expected_revision=revision,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="combined",
            )
            self.assertEqual(
                {item["decision"] for item in approved["approvals"]},
                {"definition", "design"},
            )
            replay = approve(
                root,
                change_id="C001",
                decision="definition",
                include_design=True,
                expected_revision=revision,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="combined",
            )
            self.assertFalse(replay["changed"])
            spec = root / StateStore(root).load("C001")["artifacts"]["spec"]["path"]
            spec.write_text(spec.read_text() + "\nUnrelated product wording.\n")
            git(root, "add", "docs")
            git(root, "commit", "-m", "unrelated spec")
            approvals = status(root, change_id="C001")["approvals"]
            by_decision = {item["decision"]: item for item in approvals if item["status"] != "superseded"}
            self.assertEqual(by_decision["definition"]["status"], "stale")
            self.assertEqual(by_decision["design"]["status"], "current")
            design = root / "docs/design/C001-settings.md"
            design.write_text(design.read_text() + "\nChanged layout.\n")
            git(root, "add", "docs/design")
            git(root, "commit", "-m", "design change")
            approvals = status(root, change_id="C001")["approvals"]
            design_approval = next(item for item in approvals if item["decision"] == "design")
            self.assertEqual(design_approval["status"], "stale")

    def test_architecture_marker_and_legacy_heading_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="critical", impacts=["architecture"], tickets=True)
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "architecture definition")
            first = architecture_digest(root, StateStore(root).load("C001"))
            second = architecture_digest(root, StateStore(root).load("C001"))
            self.assertEqual(first, second)
            self.assertTrue(check(root, change_id="C001", gate="definition")["ok"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            package = root / "docs/legacy"
            package.mkdir(parents=True)
            (package / "EPIC.md").write_text("# Legacy\n")
            (package / "SPEC.md").write_text(
                "# Spec\n\n## Architecture\n\nUse a bounded adapter.\n\n## Validation\n\nTest it.\n"
            )
            (package / "TICKETS.md").write_text("# Tickets\n\n## C001-T01\n\nImplement.\n")
            adopt_change(
                root,
                change_id="C001",
                slug="legacy",
                work_kind="feature",
                control_level="critical",
                impact_tags=["architecture"],
                artifacts={
                    "epic": "docs/legacy/EPIC.md",
                    "spec": "docs/legacy/SPEC.md",
                    "tickets": "docs/legacy/TICKETS.md",
                },
                ticket_statuses={"C001-T01": "planned"},
                requirement_prefixes=[],
                operation_id="adopt",
                dry_run=False,
            )
            self.assertIsNotNone(architecture_digest(root, StateStore(root).load("C001")))

    def test_architecture_approval_is_scoped_and_not_required_without_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="critical", impacts=["architecture"], tickets=True)
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "architecture definition")
            approve(
                root,
                change_id="C001",
                decision="architecture",
                expected_revision=1,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="architecture",
            )
            approve(
                root,
                change_id="C001",
                decision="definition",
                expected_revision=2,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="definition",
            )
            spec = root / StateStore(root).load("C001")["artifacts"]["spec"]["path"]
            spec.write_text(spec.read_text() + "\nUnrelated note.\n")
            git(root, "add", "docs")
            git(root, "commit", "-m", "unrelated spec")
            approvals = status(root, change_id="C001")["approvals"]
            architecture = next(item for item in approvals if item["decision"] == "architecture")
            definition = next(item for item in approvals if item["decision"] == "definition")
            self.assertEqual(architecture["status"], "current")
            self.assertEqual(definition["status"], "stale")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="critical", impacts=["concurrency"], tickets=True)
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "critical definition")
            approve(
                root,
                change_id="C001",
                decision="definition",
                expected_revision=1,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="definition",
            )
            self.assertNotIn(
                "approve-architecture",
                json.dumps(status(root, change_id="C001")["next_action"]),
            )

    def test_architecture_missing_ambiguous_and_content_drift_are_typed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="critical", impacts=["architecture"], tickets=True)
            state = StateStore(root).load("C001")
            spec = root / state["artifacts"]["spec"]["path"]
            spec.write_text("# Spec\n\n## Validation\n\nTest it.\n")
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "definition without architecture")
            missing = status(root, change_id="C001")
            self.assertEqual(
                missing["next_action"]["id"], "record-architecture-decision"
            )
            spec.write_text(
                "# Spec\n\n## Architecture\n\nFirst.\n\n"
                "## Architecture and alternatives\n\nSecond.\n"
            )
            git(root, "add", "docs")
            git(root, "commit", "-m", "ambiguous architecture")
            with self.assertRaises(IntegrityError):
                status(root, change_id="C001")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="critical", impacts=["architecture"], tickets=True)
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "architecture definition")
            approve(
                root,
                change_id="C001",
                decision="architecture",
                expected_revision=1,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="architecture",
            )
            approve(
                root,
                change_id="C001",
                decision="definition",
                expected_revision=2,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="definition",
            )
            state = StateStore(root).load("C001")
            spec = root / state["artifacts"]["spec"]["path"]
            spec_text = spec.read_text()
            architecture_start = spec_text.index("<!-- dls:architecture:start -->")
            architecture_end = spec_text.index("<!-- dls:architecture:end -->")
            spec.write_text(
                spec_text[:architecture_start]
                + "<!-- dls:architecture:start -->\n"
                + "## Architecture and alternatives\n\n"
                + "Select the actor boundary; reject a shared mutable singleton.\n"
                + spec_text[architecture_end:]
            )
            git(root, "add", "docs")
            git(root, "commit", "-m", "change architecture decision")
            approvals = status(root, change_id="C001")["approvals"]
            by_decision = {
                item["decision"]: item
                for item in approvals
                if item["decision"] in {"architecture", "definition"}
            }
            self.assertEqual(by_decision["architecture"]["status"], "stale")
            self.assertEqual(by_decision["definition"]["status"], "stale")
            self.assertEqual(
                status(root, change_id="C001")["next_action"]["id"],
                "approve-architecture",
            )

    def test_cli_design_set_and_status_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="routine", impacts=["user-interface"])
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "definition")
            cli = Path(__file__).resolve().parents[1] / "scripts/dls.py"
            environment = dict(os.environ)
            command = [
                sys.executable,
                str(cli),
                "--root",
                str(root),
                "--json",
                "design",
                "set",
                "C001",
                "--tier",
                "1",
                "--surface",
                "settings",
                "--bypass",
                "--rationale",
                "Reuse the current native settings composition.",
                "--risk",
                "low",
                "--operation-id",
                "cli-design",
            ]
            result = subprocess.run(
                command,
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            payload = json.loads(result.stdout)
            self.assertEqual(payload["design"]["tier"], 1)
            status_result = subprocess.run(
                [
                    sys.executable,
                    str(cli),
                    "--root",
                    str(root),
                    "--json",
                    "design",
                    "status",
                    "C001",
                ],
                cwd=root,
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            status_payload = json.loads(status_result.stdout)
            self.assertTrue(status_payload["design"]["bypass"])
            self.assertNotIn("native settings", status_result.stdout)

    def test_unresolved_decision_blocks_context_and_candidate_before_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = initialize_git(root)
            initialize(root)
            create_change(root, control="standard", impacts=["user-interface"])
            marker = root / "command-ran"
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text().replace(
                    "[policy]\n",
                    '[policy]\nreview_required_commands = ["test"]\n',
                    1,
                )
                + f'\n[commands.test]\nargv = ["{sys.executable}", "-c", "open(\\\"{marker}\\\", \\\"w\\\").write(\\\"ran\\\")"]\ncwd = "."\ntimeout_seconds = 5\nmax_output_bytes = 4096\nenv_allow = []\n'
            )
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "definition")
            context = build_context(
                root,
                change_id="C001",
                phase="implementation",
                include=[],
                exclude=[],
                dry_run=True,
            )
            self.assertEqual(context["next_action"]["id"], "record-design-source")
            candidate = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id="candidate",
            )
            self.assertEqual(candidate["next_action"]["id"], "record-design-source")
            self.assertFalse(marker.exists())

            status_action = status(root, change_id="C001")["next_action"]["id"]
            candidate_action = candidate_status(
                root, change_id="C001"
            )["next_action"]["id"]
            review_action = review_status(
                root, change_id="C001"
            )["next_action"]["id"]
            delivery_action = delivery_status(
                root, change_id="C001"
            )["next_action"]["id"]
            self.assertEqual(
                {
                    status_action,
                    candidate_action,
                    review_action,
                    delivery_action,
                },
                {"record-design-source"},
            )

    def test_workflow_skill_keeps_decisions_human_and_mechanics_bounded(self) -> None:
        skill_root = Path(__file__).resolve().parents[1] / "skills/dls-workflow"
        skill = (skill_root / "SKILL.md").read_text()
        gates = (skill_root / "references/gates.md").read_text()
        combined = skill + gates
        self.assertIn("approve-definition-and-design", combined)
        self.assertIn("Tier 1", combined)
        self.assertIn("Tier 3", combined)
        self.assertIn("ADR remains optional", skill)
        self.assertNotIn("mandatory Figma", combined)
        self.assertNotIn("create a bookkeeping subagent", combined)

    def test_reviewpack_receipt_and_metrics_are_bounded_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._commit_ui_definition(root)
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text().replace(
                    "[policy]\n",
                    '[policy]\nreview_required_commands = ["test"]\n',
                    1,
                )
                + f'\n[commands.test]\nargv = ["{sys.executable}", "-c", "print(\\\"pass\\\")"]\ncwd = "."\ntimeout_seconds = 5\nmax_output_bytes = 4096\nenv_allow = []\n'
            )
            git(root, "add", ".dls/config.toml")
            git(root, "commit", "-m", "validation policy")
            approve(
                root,
                change_id="C001",
                decision="definition",
                include_design=True,
                expected_revision=2,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="combined",
            )
            (root / "README.md").write_text("# Candidate\n")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate")
            candidate = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id="candidate",
            )
            pack = json.loads((root / candidate["review_pack_path"]).read_text())
            encoded = json.dumps(pack["decisions"])
            self.assertNotIn("docs/design", encoded)
            self.assertNotIn("rationale", encoded)
            self.assertEqual(pack["decisions"]["design"]["approval"], "current")
            receipt = delivery_receipt(root, change_id="C001")
            self.assertEqual(receipt["decisions"]["design"]["tier"], 2)
            metrics = review_metrics(root, change_id="C001", review_id=pack["review_id"])
            self.assertEqual(metrics["decisions"]["ui_tier"], 2)
            self.assertNotIn("docs/design", json.dumps(metrics))

            design_set(
                root,
                change_id="C001",
                tier=2,
                surfaces=["settings"],
                source_kind="external-version",
                source_ref="https://design.example.invalid/settings",
                source_version="immutable:2",
                bypass=False,
                rationale=None,
                risk=None,
                operation_id="replace-design-source",
            )
            candidate_after_drift = candidate_status(root, change_id="C001")
            review_after_drift = review_status(root, change_id="C001")
            delivery_after_drift = delivery_status(root, change_id="C001")
            self.assertFalse(candidate_after_drift["prepared"])
            self.assertFalse(review_after_drift["prepared"])
            self.assertEqual(
                review_after_drift["next_action"]["id"],
                "approve-definition-and-design",
            )
            self.assertEqual(
                delivery_after_drift["next_action"]["id"],
                "approve-definition-and-design",
            )

    def test_legacy_whole_definition_decision_approval_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            create_change(root, control="routine", impacts=["user-interface"])
            store = StateStore(root)
            state = store.load("C001")
            state["approvals"].append(
                {
                    "id": "legacy-design",
                    "decision": "design",
                    "object_digest": current_definition_digest(root, state),
                    "git_sha": None,
                    "actor": "user",
                    "authority": "user",
                    "recorded_at": "2026-01-01T00:00:00Z",
                    "status": "current",
                }
            )
            store.path("C001").write_text(json.dumps(state))
            projected = status(root, change_id="C001")
            legacy = next(item for item in projected["approvals"] if item["id"] == "legacy-design")
            self.assertEqual(legacy["status"], "current")
            self.assertEqual(projected["next_action"]["id"], "record-design-source")


if __name__ == "__main__":
    unittest.main()
