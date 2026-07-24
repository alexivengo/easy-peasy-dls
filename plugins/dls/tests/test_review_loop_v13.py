from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dls_core.errors import IntegrityError, UsageError
from dls_core.operations import (
    _review_pack_digest,
    _risk_lenses,
    _validate_review_pack,
    _validate_review_report,
    approve,
    check,
    evidence_add,
    finding_disposition,
    remediation_start,
    review_import,
    review_pack,
    review_ready,
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


class ReviewLoopV13Tests(unittest.TestCase):
    def _first_not_clear(self, root: Path) -> tuple[str, dict, dict]:
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
        git(root, "commit", "-m", "candidate one")
        head_sha = git(root, "rev-parse", "HEAD")
        evidence_add(
            root,
            change_id="C001",
            command_id="test",
            exit_code=0,
            summary="PASS candidate one",
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
            operation_id="pack-1",
        )
        started = start_review_with_fake_codex(
            root,
            change_id="C001",
            operation_id="start-1",
        )
        finding = {
            "id": "R001",
            "severity": "should-fix",
            "kind": "defect",
            "location": "README.md:1",
            "issue": "The candidate omits the required boundary.",
            "impact": "The contract can be violated.",
            "required_fix": "Implement and regression-test the boundary.",
            "base_sha": pack["review_pack"]["base_sha"],
            "head_sha": pack["review_pack"]["head_sha"],
            "blocks": ["review", "acceptance"],
        }
        report = build_review_report(
            root,
            pack_result=pack,
            start_result=started,
            verdict="not-clear",
            findings=[finding],
        )
        report_path = root / ".dls/cache/review-one.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        imported = review_import(
            root,
            change_id="C001",
            report_path=".dls/cache/review-one.json",
            expected_revision=5,
            operation_id="import-1",
        )
        self.assertEqual(imported["verdict"], "not-clear")
        return base_sha, pack, finding

    def _prepare_remediation_candidate(
        self,
        root: Path,
        *,
        base_sha: str,
    ) -> tuple[dict, dict]:
        manifest = remediation_start(root, change_id="C001")
        self.assertEqual(
            [
                item["path"]
                for item in manifest["remediation_manifest"]["inputs"]
                if "/results/" in item["path"]
            ],
            [".dls/reviews/C001/results/" + manifest["review_id"] + ".json"],
        )
        (root / "README.md").write_text("# Fixture\n\nBoundary fixed.\n", encoding="utf-8")
        git(root, "add", "README.md")
        git(root, "commit", "-m", "fix review finding")
        head_sha = git(root, "rev-parse", "HEAD")
        evidence = evidence_add(
            root,
            change_id="C001",
            command_id="test",
            exit_code=0,
            summary="PASS candidate two",
            expected_revision=6,
            git_sha=head_sha,
            artifacts=[],
            environment="fixture",
            duration_seconds=0.1,
            operation_id="evidence-2",
        )
        addressed = finding_disposition(
            root,
            change_id="C001",
            finding_id="R001",
            disposition_status="addressed",
            rationale="Implemented the boundary and added current evidence.",
            expected_revision=7,
            git_sha=head_sha,
            evidence=[evidence["evidence_path"]],
            actor="codex",
            prompt=None,
            response=None,
            operation_id="address-1",
        )
        self.assertEqual(addressed["disposition"]["status"], "addressed")
        ready = review_ready(
            root,
            change_id="C001",
            base_ref=base_sha,
            expected_revision=8,
            operation_id="ready-2",
        )
        self.assertTrue(ready["ok"])
        self.assertEqual(ready["review_pack"]["review_mode"], "remediation")
        return ready, evidence

    def test_latest_only_remediation_manifest_rejects_stale_and_tampered_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._first_not_clear(root)
            manifest = remediation_start(root, change_id="C001")
            self.assertEqual(len(manifest["remediation_manifest"]["open_findings"]), 1)
            result_path = root / manifest["remediation_manifest"]["review_result_path"]
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            payload["verdict"] = "review-clear"
            result_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "digest"):
                remediation_start(root, change_id="C001")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._first_not_clear(root)
            manifest = remediation_start(root, change_id="C001")
            (root / manifest["remediation_manifest"]["review_result_path"]).unlink()
            with self.assertRaisesRegex(IntegrityError, "Missing JSON"):
                remediation_start(root, change_id="C001")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._first_not_clear(root)
            (root / "README.md").write_text("# stale\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "advance before remediation")
            with self.assertRaisesRegex(IntegrityError, "stale"):
                remediation_start(root, change_id="C001")

    def test_review_ready_requires_addressed_findings_and_current_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha, _, _ = self._first_not_clear(root)
            remediation_start(root, change_id="C001")
            (root / "README.md").write_text("# fixed\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate without evidence")
            blocked = review_ready(
                root,
                change_id="C001",
                base_ref=base_sha,
                expected_revision=6,
                operation_id="ready-blocked",
            )
            self.assertFalse(blocked["ok"])
            self.assertEqual(
                blocked["next_action"]["id"],
                "run-review-validation",
            )

    def test_review_ready_returns_recoverable_action_when_manifest_was_not_started(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha, _, _ = self._first_not_clear(root)
            (root / "README.md").write_text("# changed too early\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "advance without remediation manifest")
            blocked = review_ready(
                root,
                change_id="C001",
                base_ref=base_sha,
                expected_revision=6,
                operation_id="ready-without-manifest",
                dry_run=True,
            )
            self.assertFalse(blocked["ok"])
            self.assertTrue(blocked["dry_run"])
            self.assertEqual(
                blocked["next_action"]["id"],
                "restore-reviewed-head-and-run-remediation-start",
            )

    def test_delta_review_requires_prior_verdict_and_final_full_before_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha, first_pack, _ = self._first_not_clear(root)
            ready, _ = self._prepare_remediation_candidate(root, base_sha=base_sha)
            pack = ready["review_pack"]
            self.assertEqual(
                pack["comparison_base_sha"],
                first_pack["review_pack"]["head_sha"],
            )
            self.assertEqual(pack["risk_lenses"], [])
            self.assertLessEqual(
                len(pack["changed_files"]),
                len(pack["full_changed_files"]),
            )
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="start-2",
            )
            self.assertEqual(
                [item["base_sha"] for item in started["native_coverage"]],
                [base_sha, first_pack["review_pack"]["head_sha"]],
            )
            clear_report = build_review_report(
                root,
                pack_result=ready,
                start_result=started,
                verdict="review-clear",
            )
            incomplete = json.loads(json.dumps(clear_report))
            incomplete["prior_finding_verdicts"] = []
            incomplete_path = root / ".dls/cache/incomplete-prior.json"
            incomplete_path.write_text(json.dumps(incomplete), encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "prior finding verdicts"):
                review_import(
                    root,
                    change_id="C001",
                    report_path=".dls/cache/incomplete-prior.json",
                    expected_revision=10,
                    operation_id="import-incomplete-prior",
                )
            no_final = json.loads(json.dumps(clear_report))
            no_final["lanes"]["semantic"]["passes"] = no_final["lanes"]["semantic"][
                "passes"
            ][:1]
            no_final_path = root / ".dls/cache/no-final-full.json"
            no_final_path.write_text(json.dumps(no_final), encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "final-full"):
                review_import(
                    root,
                    change_id="C001",
                    report_path=".dls/cache/no-final-full.json",
                    expected_revision=10,
                    operation_id="import-no-final",
                )
            report_path = root / ".dls/cache/review-two.json"
            report_path.write_text(json.dumps(clear_report), encoding="utf-8")
            imported = review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/review-two.json",
                expected_revision=10,
                operation_id="import-2",
            )
            self.assertTrue(imported["ok"])
            state = StateStore(root).load("C001")
            verified = [
                item
                for item in state["finding_dispositions"]
                if item.get("finding_id") == "R001"
                and item.get("status") == "verified"
            ]
            self.assertEqual(len(verified), 1)
            self.assertTrue(check(root, change_id="C001", gate="accept")["ok"])

    def test_tampered_remediation_manifest_blocks_repeat_review_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha, _, _ = self._first_not_clear(root)
            ready, _ = self._prepare_remediation_candidate(root, base_sha=base_sha)
            manifest_path = root / ready["review_pack"]["remediation_manifest"][
                "manifest_path"
            ]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["open_findings"][0]["issue"] = "tampered"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "manifest digest"):
                start_review_with_fake_codex(
                    root,
                    change_id="C001",
                    operation_id="start-tampered-manifest",
                )

    def test_second_remediation_context_contains_only_latest_review_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha, _, _ = self._first_not_clear(root)
            ready, _ = self._prepare_remediation_candidate(root, base_sha=base_sha)
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="start-2",
            )
            finding = {
                "id": "R002",
                "severity": "should-fix",
                "kind": "validation-gap",
                "location": "README.md:3",
                "issue": "The remediation needs one additional regression proof.",
                "impact": "The repaired boundary could regress.",
                "required_fix": "Add the focused proof.",
                "base_sha": ready["review_pack"]["base_sha"],
                "head_sha": ready["review_pack"]["head_sha"],
                "blocks": ["review", "acceptance"],
            }
            report = build_review_report(
                root,
                pack_result=ready,
                start_result=started,
                verdict="not-clear",
                findings=[finding],
            )
            report_path = root / ".dls/cache/review-two-not-clear.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            imported = review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/review-two-not-clear.json",
                expected_revision=10,
                operation_id="import-2-not-clear",
            )
            manifest = remediation_start(root, change_id="C001")
            result_inputs = [
                item["path"]
                for item in manifest["remediation_manifest"]["inputs"]
                if "/results/" in item["path"]
            ]
            self.assertEqual(result_inputs, [imported["review_result_path"]])

    def test_current_evidence_is_deduplicated_and_stage_requirements_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha = initialize_git(root)
            initialize(root)
            config_path = root / ".dls/config.toml"
            config = config_path.read_text(encoding="utf-8")
            config = config.replace(
                "max_agent_depth = 1",
                "max_agent_depth = 1\n"
                'review_required_commands = ["test"]\n'
                'acceptance_required_commands = ["lint"]',
            )
            config += (
                "\n[commands.test]\n"
                'argv = ["true"]\n'
                'cwd = "."\n'
                "timeout_seconds = 30\n"
                "max_output_bytes = 4096\n"
                "env_allow = []\n"
                "\n[commands.lint]\n"
                'argv = ["true"]\n'
                'cwd = "."\n'
                "timeout_seconds = 30\n"
                "max_output_bytes = 4096\n"
                "env_allow = []\n"
            )
            config_path.write_text(config, encoding="utf-8")
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
            git(root, "commit", "-m", "candidate")
            head_sha = git(root, "rev-parse", "HEAD")
            first_test = evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=0,
                summary="old pass",
                expected_revision=2,
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="test-old",
            )
            failed_test = evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=1,
                summary="latest failure",
                expected_revision=3,
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="test-failed",
            )
            lint = evidence_add(
                root,
                change_id="C001",
                command_id="lint",
                exit_code=0,
                summary="lint pass",
                expected_revision=4,
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="lint-pass",
            )
            blocked = review_ready(
                root,
                change_id="C001",
                base_ref=base_sha,
                expected_revision=5,
                operation_id="ready-blocked",
            )
            self.assertEqual(
                blocked["next_action"]["id"],
                "run-review-validation",
            )
            current_test = evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=0,
                summary="current pass",
                expected_revision=5,
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="test-current",
            )
            ready = review_ready(
                root,
                change_id="C001",
                base_ref=base_sha,
                expected_revision=6,
                operation_id="ready",
            )
            self.assertTrue(ready["ok"])
            self.assertEqual(
                set(ready["review_pack"]["evidence"]),
                {current_test["evidence_path"], lint["evidence_path"]},
            )
            self.assertNotIn(
                first_test["evidence_path"],
                ready["review_pack"]["evidence"],
            )
            self.assertNotIn(
                failed_test["evidence_path"],
                ready["review_pack"]["evidence"],
            )

    def test_implementer_cannot_record_verified_and_legacy_resolved_is_addressed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._first_not_clear(root)
            evidence_path = StateStore(root).load("C001")["evidence"][-1]
            legacy = finding_disposition(
                root,
                change_id="C001",
                finding_id="R001",
                disposition_status="resolved",
                rationale="Legacy caller claims the fix.",
                expected_revision=6,
                git_sha=git(root, "rev-parse", "HEAD"),
                evidence=[evidence_path],
                actor="codex",
                prompt=None,
                response=None,
                operation_id="legacy-resolved",
            )
            self.assertEqual(legacy["disposition"]["status"], "addressed")
            with self.assertRaises(UsageError):
                finding_disposition(
                    root,
                    change_id="C001",
                    finding_id="R001",
                    disposition_status="verified",
                    rationale="Self verification is forbidden.",
                    expected_revision=7,
                    git_sha=git(root, "rev-parse", "HEAD"),
                    evidence=[evidence_path],
                    actor="codex",
                    prompt=None,
                    response=None,
                    operation_id="self-verified",
                )

    def test_critical_risk_lenses_are_deterministic_and_bounded(self) -> None:
        state = {
            "control_level": "critical",
            "impact_tags": [
                "architecture",
                "availability",
                "compatibility",
                "concurrency",
                "data-loss",
                "data-migration",
                "external-dependency",
                "public-api",
                "release",
                "security-privacy",
                "user-interface",
            ],
        }
        first = _risk_lenses(state)
        second = _risk_lenses(state)
        self.assertEqual(first, second)
        self.assertEqual(
            [item["id"] for item in first],
            ["contract-trust", "concurrency-reliability", "data-migration"],
        )
        self.assertEqual(len(first), 3)
        self.assertNotIn("release", json.dumps(first))

    def test_historical_reviewpack_and_reviewir_v1_remain_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, pack_result, _ = self._first_not_clear(root)
            pack = json.loads(json.dumps(pack_result["review_pack"]))
            for field in (
                "review_mode",
                "epic_base_sha",
                "comparison_base_sha",
                "epic_merge_base",
                "prior_review",
                "remediation_manifest",
                "risk_lenses",
                "required_prior_findings",
                "prior_native_coverage",
                "full_changed_files",
            ):
                pack.pop(field)
            pack["schema_version"] = 1
            pack["pack_digest"] = _review_pack_digest(pack)
            _validate_review_pack(pack, "C001")

            result_entry = next(
                item
                for item in StateStore(root).load("C001")["reviews"]
                if item.get("kind") == "result"
            )
            report = json.loads(
                (root / result_entry["result_path"]).read_text(encoding="utf-8")
            )
            report["schema_version"] = 1
            report["pack_digest"] = pack["pack_digest"]
            report.pop("review_mode", None)
            report.pop("comparison_base_sha", None)
            report.pop("prior_finding_verdicts", None)
            report["lanes"]["semantic"].pop("passes", None)
            report["lanes"].pop("specialists", None)
            _validate_review_report(report, "C001", pack)


if __name__ == "__main__":
    unittest.main()
