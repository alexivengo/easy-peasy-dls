from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dls_core.errors import IntegrityError, UsageError
from dls_core.operations import (
    adopt_change,
    approve,
    build_context,
    check,
    evidence_add,
    finding_disposition,
    new_change,
    review_import,
    review_pack,
    revoke_approval,
    status,
    ticket_set,
)
from dls_core.state import StateStore

from support import (
    build_review_report,
    create_change,
    git,
    initialize,
    initialize_git,
    start_review_with_fake_codex,
)


class WorkflowTests(unittest.TestCase):
    def test_adopt_existing_critical_package_without_rewriting_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            package = root / "docs/epics/EPIC-01-existing"
            package.mkdir(parents=True)
            artifacts = {
                "epic": "docs/epics/EPIC-01-existing/EPIC.md",
                "spec": "docs/epics/EPIC-01-existing/SPEC.md",
                "tickets": "docs/epics/EPIC-01-existing/TICKETS.md",
                "traceability": "docs/epics/REQUIREMENTS-MATRIX.json",
            }
            (root / artifacts["epic"]).write_text(
                "# EPIC-01\n\nDeliver F-01, N-01, and C-01.\n",
                encoding="utf-8",
            )
            (root / artifacts["spec"]).write_text(
                "# Contract\n\n- F-01: behavior\n- N-01: quality\n- C-01: constraint\n",
                encoding="utf-8",
            )
            (root / artifacts["tickets"]).write_text(
                "# Tickets\n\n"
                "## EPIC-01-T01\n\nImplements F-01.\n\n"
                "## EPIC-01-T02\n\nCompletes the package.\n",
                encoding="utf-8",
            )
            (root / artifacts["traceability"]).write_text(
                json.dumps(
                    {
                        "EPIC-01-existing": {
                            "F-01": {"producerTicket": "EPIC-01-T01"},
                            "N-01": {"producerTicket": "EPIC-01-T02"},
                            "C-01": {"producerTicket": "EPIC-01-T02"},
                        },
                        "OTHER": {
                            "F-99": {"producerTicket": "OTHER-T01"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            before = {
                relative: (root / relative).read_bytes()
                for relative in artifacts.values()
            }
            adopted = adopt_change(
                root,
                change_id="EPIC-01",
                slug="existing",
                work_kind="feature",
                control_level="critical",
                impact_tags=["public-api", "architecture"],
                artifacts=artifacts,
                ticket_statuses={
                    "EPIC-01-T01": "done",
                    "EPIC-01-T02": "implemented",
                },
                requirement_prefixes=["F", "N", "C"],
                operation_id="adopt-epic-01",
                dry_run=False,
            )
            self.assertTrue(adopted["changed"])
            definition_before_unrelated_change = status(
                root,
                change_id="EPIC-01",
            )["definition_digest"]
            self.assertEqual(
                {
                    relative: (root / relative).read_bytes()
                    for relative in artifacts.values()
                },
                before,
            )
            self.assertTrue(check(root, change_id="EPIC-01", gate="definition")["ok"])
            stored = StateStore(root).load("EPIC-01")
            self.assertTrue(stored["adopted"])
            self.assertEqual(stored["requirement_prefixes"], ["C", "F", "N"])
            self.assertEqual(
                stored["artifacts"]["traceability"]["producer_ticket_scope"],
                ["EPIC-01-T01", "EPIC-01-T02"],
            )
            self.assertEqual(stored["tickets"]["EPIC-01-T01"]["status"], "done")
            matrix_path = root / artifacts["traceability"]
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
            matrix["OTHER"]["F-99"]["status"] = "changed outside this epic"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            self.assertEqual(
                status(root, change_id="EPIC-01")["definition_digest"],
                definition_before_unrelated_change,
            )
            matrix["EPIC-01-existing"]["F-01"]["status"] = "changed in this epic"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            self.assertNotEqual(
                status(root, change_id="EPIC-01")["definition_digest"],
                definition_before_unrelated_change,
            )
            transitioned = ticket_set(
                root,
                change_id="EPIC-01",
                ticket_id="EPIC-01-T02",
                ticket_status="validated",
                expected_revision=1,
                note="Current evidence passed.",
                operation_id="validate-epic-01-t02",
            )
            self.assertEqual(transitioned["ticket"]["status"], "validated")
            replayed = adopt_change(
                root,
                change_id="EPIC-01",
                slug="ignored-on-replay",
                work_kind="feature",
                control_level="critical",
                impact_tags=[],
                artifacts=artifacts,
                ticket_statuses={
                    "EPIC-01-T01": "done",
                    "EPIC-01-T02": "implemented",
                },
                requirement_prefixes=[],
                operation_id="adopt-epic-01",
                dry_run=False,
            )
            self.assertFalse(replayed["changed"])

    def test_adopt_requires_exact_ticket_status_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            package = root / "docs/epics/E01-existing"
            package.mkdir(parents=True)
            (package / "EPIC.md").write_text("# Epic\n", encoding="utf-8")
            (package / "SPEC.md").write_text("# Spec\n", encoding="utf-8")
            (package / "TICKETS.md").write_text(
                "# Tickets\n\n## E01-T01\n\nWork.\n",
                encoding="utf-8",
            )
            with self.assertRaises(UsageError):
                adopt_change(
                    root,
                    change_id="E01",
                    slug="existing",
                    work_kind="feature",
                    control_level="critical",
                    impact_tags=[],
                    artifacts={
                        "epic": "docs/epics/E01-existing/EPIC.md",
                        "spec": "docs/epics/E01-existing/SPEC.md",
                        "tickets": "docs/epics/E01-existing/TICKETS.md",
                    },
                    ticket_statuses={},
                    requirement_prefixes=[],
                    operation_id="adopt-e01",
                    dry_run=False,
                )
            self.assertFalse((root / ".dls/state/E01.json").exists())

    def test_minimal_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            micro = new_change(
                root,
                change_id="M001",
                slug="micro",
                title="Micro",
                work_kind="chore",
                control_level="micro",
                impact_tags=[],
                roadmap_epic=False,
                with_tickets=False,
                with_adr=False,
                outcome="Tiny correction.",
                operation_id=None,
                dry_run=False,
            )
            self.assertEqual(micro["artifacts"], [])
            self.assertFalse((root / ".dls/state/M001.json").exists())

            routine = create_change(root, change_id="R001", control="routine")
            standard = create_change(root, change_id="S001", control="standard")
            standard_tickets = create_change(
                root,
                change_id="S002",
                control="standard",
                tickets=True,
            )
            critical = create_change(
                root,
                change_id="K001",
                control="critical",
                adr=True,
            )
            self.assertEqual([Path(item).name for item in routine["artifacts"]], ["CHANGE.md"])
            self.assertEqual([Path(item).name for item in standard["artifacts"]], ["SPEC.md"])
            self.assertEqual(
                [Path(item).name for item in standard_tickets["artifacts"]],
                ["SPEC.md", "TICKETS.md"],
            )
            self.assertEqual(
                [Path(item).name for item in critical["artifacts"]],
                ["EPIC.md", "SPEC.md", "TICKETS.md", "ADR.md"],
            )
            self.assertIn(
                "architecture:decision",
                [
                    item["id"]
                    for item in check(root, change_id="K001", gate="review")["checks"]
                    if not item["ok"]
                ],
            )
            for result in (routine, standard, standard_tickets, critical):
                for relative in result["artifacts"]:
                    self.assertNotIn("{{", (root / relative).read_text())

    def test_scoped_approval_and_authored_digest_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            create_change(root, control="standard")
            current = status(root, change_id="C001")
            digest = current["definition_digest"]
            with self.assertRaises(IntegrityError):
                approve(
                    root,
                    change_id="C001",
                    decision="definition",
                    expected_revision=1,
                    actor="codex",
                    prompt="Approve it?",
                    response="Да",
                    git_sha=None,
                    conditions=None,
                    operation_id="approve-definition",
                )
            approved = approve(
                root,
                change_id="C001",
                decision="definition",
                expected_revision=1,
                actor="codex",
                prompt=f"Approve definition package {digest[:8]}?",
                response="Да, подтверждаю",
                git_sha=None,
                conditions=None,
                operation_id="approve-definition",
            )
            self.assertEqual(approved["state_revision"], 2)
            replayed = approve(
                root,
                change_id="C001",
                decision="definition",
                expected_revision=1,
                actor="codex",
                prompt=f"Approve definition package {digest[:8]}?",
                response="Да",
                git_sha=None,
                conditions=None,
                operation_id="approve-definition",
            )
            self.assertFalse(replayed["changed"])
            spec = root / "docs/changes/C001-c001-change/SPEC.md"
            original = spec.read_text(encoding="utf-8")
            spec.write_text(
                original
                + "\n<!-- DLS:GENERATED:START -->\n"
                + "Derived review status only.\n"
                + "<!-- DLS:GENERATED:END -->\n",
                encoding="utf-8",
            )
            self.assertEqual(
                status(root, change_id="C001")["approvals"][-1]["status"],
                "current",
            )
            evidence_add(
                root,
                change_id="C001",
                command_id="state-only-proof",
                exit_code=0,
                summary="PASS",
                expected_revision=2,
                git_sha=None,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="state-only-evidence",
            )
            self.assertEqual(
                status(root, change_id="C001")["approvals"][-1]["status"],
                "current",
            )
            spec.write_text(spec.read_text() + "\nMaterial change.\n", encoding="utf-8")
            approvals = status(root, change_id="C001")["approvals"]
            self.assertEqual(approvals[-1]["status"], "stale")

    def test_ui_requires_design_decision_but_routine_skips_definition_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            create_change(
                root,
                control="routine",
                impacts=["user-interface"],
            )
            evidence_add(
                root,
                change_id="C001",
                command_id="focused-test",
                exit_code=0,
                summary="PASS",
                expected_revision=1,
                git_sha=None,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="evidence-1",
            )
            blocked = check(root, change_id="C001", gate="accept")
            self.assertIn(
                "ui:design-decision",
                [item["id"] for item in blocked["checks"] if not item["ok"]],
            )
            revision = status(root, change_id="C001")["state_revision"]
            approve(
                root,
                change_id="C001",
                decision="design",
                expected_revision=revision,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions="Use exact existing settings row as Tier 1 precedent.",
                operation_id="design-1",
            )
            accepted = check(root, change_id="C001", gate="accept")
            self.assertTrue(accepted["ok"])
            ids = [item["id"] for item in accepted["checks"]]
            self.assertNotIn("definition:approved", ids)
            self.assertNotIn("review:clear", ids)

    def test_approval_revocation_is_scoped_and_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            create_change(root, control="standard")
            digest = status(root, change_id="C001")["definition_digest"]
            approval = approve(
                root,
                change_id="C001",
                decision="definition",
                expected_revision=1,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="definition-1",
            )["approval"]
            with self.assertRaises(IntegrityError):
                revoke_approval(
                    root,
                    change_id="C001",
                    approval_id=approval["id"],
                    expected_revision=2,
                    actor="codex",
                    prompt="Revoke it?",
                    response="Да",
                    rationale="Scope changed.",
                    operation_id="revoke-1",
                )
            revoked = revoke_approval(
                root,
                change_id="C001",
                approval_id=approval["id"],
                expected_revision=2,
                actor="codex",
                prompt=f"revoke {approval['id']} at {digest[:8]}?",
                response="Да, подтверждаю",
                rationale="Scope changed.",
                operation_id="revoke-1",
            )
            self.assertEqual(revoked["approval"]["decision"], "revoke")
            stored = StateStore(root).load("C001")
            target = next(item for item in stored["approvals"] if item["id"] == approval["id"])
            self.assertEqual(target["status"], "revoked")
            self.assertEqual(stored["lifecycle"], "draft")

    def test_ticket_transitions_and_context_determinism(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            create_change(root, control="standard", tickets=True)
            first = ticket_set(
                root,
                change_id="C001",
                ticket_id="T01",
                ticket_status="in-progress",
                expected_revision=1,
                note=None,
                operation_id="ticket-1",
            )
            self.assertIn(
                "tickets:implemented-for-review",
                [
                    item["id"]
                    for item in check(root, change_id="C001", gate="review")["checks"]
                    if not item["ok"]
                ],
            )
            with self.assertRaises(IntegrityError):
                ticket_set(
                    root,
                    change_id="C001",
                    ticket_id="T01",
                    ticket_status="done",
                    expected_revision=first["state_revision"],
                    note=None,
                    operation_id="ticket-2",
                )
            blocked_context = build_context(
                root,
                change_id="C001",
                phase="implementation",
                include=[],
                exclude=[],
                dry_run=True,
            )
            self.assertEqual(blocked_context["status"], "blocked")
            self.assertIsNone(blocked_context["manifest"])
            self.assertEqual(
                blocked_context["next_action"]["id"], "approve-definition"
            )
            approve(
                root,
                change_id="C001",
                decision="definition",
                expected_revision=first["state_revision"],
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="definition-before-context",
            )
            context_one = build_context(
                root,
                change_id="C001",
                phase="implementation",
                include=[],
                exclude=[],
            )
            context_two = build_context(
                root,
                change_id="C001",
                phase="implementation",
                include=[],
                exclude=[],
            )
            self.assertEqual(
                context_one["manifest"]["manifest_digest"],
                context_two["manifest"]["manifest_digest"],
            )
            with self.assertRaises(IntegrityError):
                build_context(
                    root,
                    change_id="C001",
                    phase="implementation",
                    include=[],
                    exclude=[".dls/config.toml"],
                )

    def test_evidence_redaction_staleness_and_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root)
            evidence = evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=0,
                summary="token=very-secret-value",
                expected_revision=1,
                git_sha=git(root, "rev-parse", "HEAD"),
                artifacts=[],
                environment="password=hunter2",
                duration_seconds=0.2,
                operation_id="evidence-1",
            )
            payload = json.loads((root / evidence["evidence_path"]).read_text())
            self.assertNotIn("very-secret-value", json.dumps(payload))
            self.assertNotIn("hunter2", json.dumps(payload))
            replayed = evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=99,
                summary="different retry input",
                expected_revision=1,
                git_sha=None,
                artifacts=[],
                environment=None,
                duration_seconds=None,
                operation_id="evidence-1",
            )
            self.assertFalse(replayed["changed"])
            self.assertEqual(replayed["evidence"]["exit_code"], 0)
            (root / "README.md").write_text("# Changed\n", encoding="utf-8")
            result = check(root, change_id="C001", gate="accept")
            failed = [item["id"] for item in result["checks"] if not item["ok"]]
            self.assertIn("validation:passing-evidence", failed)

    def test_exact_revision_review_import_and_staleness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha = initialize_git(root)
            initialize(root)
            create_change(root, control="standard")
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
                operation_id="definition-1",
            )
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "approved definition")
            head_sha = git(root, "rev-parse", "HEAD")
            evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=0,
                summary="PASS",
                expected_revision=2,
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="evidence-1",
            )
            pack = review_pack(
                root,
                change_id="C001",
                base_ref=base_sha,
                head_ref=None,
                expected_revision=3,
                advisory_dirty=False,
                operation_id="review-pack-1",
            )
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="review-start-1",
            )
            report = build_review_report(
                root,
                pack_result=pack,
                start_result=started,
                verdict="review-clear",
            )
            report_path = root / ".dls/cache/review.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report), encoding="utf-8")
            imported = review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/review.json",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                operation_id="review-import-1",
            )
            self.assertTrue(imported["ok"])
            self.assertTrue(check(root, change_id="C001", gate="accept")["ok"])
            (root / "README.md").write_text("# Changed after review\n", encoding="utf-8")
            stale = check(root, change_id="C001", gate="accept")
            self.assertFalse(stale["ok"])

    def test_finding_disposition_close_reopen_and_scoped_waiver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha = initialize_git(root)
            initialize(root)
            create_change(root, control="standard")
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
                operation_id="definition-1",
            )
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "approved definition")
            head_sha = git(root, "rev-parse", "HEAD")
            evidence = evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=0,
                summary="PASS",
                expected_revision=2,
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="evidence-1",
            )
            bridge_evidence = evidence_add(
                root,
                change_id="C001",
                command_id="bridge-test",
                exit_code=0,
                summary="PASS bridge",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="evidence-bridge-1",
            )
            pack = review_pack(
                root,
                change_id="C001",
                base_ref=base_sha,
                head_ref=None,
                expected_revision=StateStore(root).load("C001")["state_revision"],
                advisory_dirty=False,
                operation_id="review-pack-1",
            )
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="review-start-1",
            )
            finding = {
                "id": "F001",
                "severity": "should-fix",
                "kind": "validation-gap",
                "location": "tests",
                "issue": "Missing boundary proof.",
                "impact": "Regression risk remains.",
                "required_fix": "Add proof or explicitly accept the risk.",
                "base_sha": pack["review_pack"]["base_sha"],
                "head_sha": pack["review_pack"]["head_sha"],
            }
            report = build_review_report(
                root,
                pack_result=pack,
                start_result=started,
                verdict="not-clear",
                findings=[finding],
            )
            report_path = root / ".dls/cache/review-finding.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report), encoding="utf-8")
            review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/review-finding.json",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                operation_id="review-import-1",
            )
            resolved = finding_disposition(
                root,
                change_id="C001",
                finding_id="F001",
                disposition_status="resolved",
                rationale="Boundary proof is attached.",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                git_sha=head_sha,
                evidence=[
                    evidence["evidence_path"],
                    bridge_evidence["evidence_path"],
                    evidence["evidence_path"],
                ],
                actor="codex",
                prompt=None,
                response=None,
                operation_id="finding-resolved",
            )
            self.assertEqual(resolved["disposition"]["status"], "addressed")
            self.assertEqual(resolved["disposition"]["legacy_alias"], "resolved")
            self.assertEqual(
                resolved["disposition"]["evidence"],
                [evidence["evidence_path"], bridge_evidence["evidence_path"]],
            )
            self.assertEqual(resolved["evidence_count"], 2)
            reopened = finding_disposition(
                root,
                change_id="C001",
                finding_id="F001",
                disposition_status="reopened",
                rationale="The attached proof does not exercise the boundary.",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                git_sha=head_sha,
                evidence=[],
                actor="codex",
                prompt=None,
                response=None,
                operation_id="finding-reopened",
            )
            self.assertEqual(reopened["disposition"]["status"], "reopened")
            with self.assertRaises(IntegrityError):
                finding_disposition(
                    root,
                    change_id="C001",
                    finding_id="F001",
                    disposition_status="waived",
                    rationale="Risk accepted.",
                    expected_revision=StateStore(root).load("C001")[
                        "state_revision"
                    ],
                    git_sha=head_sha,
                    evidence=[evidence["evidence_path"]],
                    actor="codex",
                    prompt="Waive it?",
                    response="Да",
                    operation_id="finding-waive",
                )
            waived = finding_disposition(
                root,
                change_id="C001",
                finding_id="F001",
                disposition_status="waived",
                rationale="The user accepts the bounded residual risk.",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                git_sha=head_sha,
                evidence=[evidence["evidence_path"]],
                actor="codex",
                prompt=f"waive F001 at {head_sha[:8]}?",
                response="Да, принимаю",
                operation_id="finding-waive",
            )
            self.assertEqual(waived["disposition"]["authority"], "user")

    def test_dirty_review_is_advisory_and_cannot_clear_standard_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha = initialize_git(root)
            initialize(root)
            create_change(root, control="standard")
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
                operation_id="definition-1",
            )
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "approved definition")
            head_sha = git(root, "rev-parse", "HEAD")
            evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=0,
                summary="PASS",
                expected_revision=2,
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="evidence-1",
            )
            (root / "README.md").write_text("# Dirty\n", encoding="utf-8")
            with self.assertRaises(IntegrityError):
                review_pack(
                    root,
                    change_id="C001",
                    base_ref=base_sha,
                    head_ref=None,
                    expected_revision=3,
                    advisory_dirty=False,
                    operation_id="review-pack-rejected",
                )
            pack = review_pack(
                root,
                change_id="C001",
                base_ref=base_sha,
                head_ref=None,
                expected_revision=3,
                advisory_dirty=True,
                operation_id="review-pack-advisory",
            )
            self.assertEqual(pack["review_pack"]["mode"], "advisory-dirty")
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="review-start-advisory",
            )
            report = build_review_report(
                root,
                pack_result=pack,
                start_result=started,
                verdict="review-clear",
            )
            report_path = root / ".dls/cache/advisory-review.json"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report), encoding="utf-8")
            review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/advisory-review.json",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                operation_id="review-import-advisory",
            )
            failed = check(root, change_id="C001", gate="accept")
            self.assertFalse(failed["ok"])
            self.assertIn(
                "review:clear",
                [item["id"] for item in failed["checks"] if not item["ok"]],
            )


if __name__ == "__main__":
    unittest.main()
