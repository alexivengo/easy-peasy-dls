from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dls_core.operations import (
    _active_prior_findings,
    _all_review_findings,
    _canonical_review_findings,
    _open_finding_counts,
    approve,
    evidence_add,
    finding_disposition,
    remediation_start,
    review_import,
    review_pack,
    review_ready,
    review_start,
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


class ReviewRecoveryV021Tests(unittest.TestCase):
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
            "ticket_ids": [],
            "requirement_ids": [],
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
        review_import(
            root,
            change_id="C001",
            report_path=".dls/cache/review-one.json",
            expected_revision=5,
            operation_id="import-1",
        )
        return base_sha, pack, finding

    def _advance_candidate(
        self,
        root: Path,
        *,
        disposition_status: str,
    ) -> str:
        remediation_start(root, change_id="C001")
        (root / "README.md").write_text("# Fixture\n\nCandidate two.\n", encoding="utf-8")
        git(root, "add", "README.md")
        git(root, "commit", "-m", "candidate two")
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
        finding_disposition(
            root,
            change_id="C001",
            finding_id="R001",
            disposition_status=disposition_status,
            rationale="Send the current candidate to independent adjudication.",
            expected_revision=7,
            git_sha=head_sha,
            evidence=[evidence["evidence_path"]]
            if disposition_status == "addressed"
            else [],
            actor="codex",
            prompt=None,
            response=None,
            operation_id=f"disposition-{disposition_status}",
        )
        return head_sha

    def test_first_review_without_pack_returns_provide_review_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
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
            git(root, "commit", "-m", "candidate")

            result = review_start(
                root,
                change_id="C001",
                pack_path=None,
                operation_id="first-review-without-pack",
                dry_run=True,
            )

            self.assertFalse(result["ok"])
            self.assertFalse(result["pack_created"])
            self.assertEqual(result["next_action"]["id"], "provide-review-base")
            self.assertEqual(StateStore(root).load("C001")["reviews"], [])

    def test_repeat_review_auto_prepares_and_ignores_stale_zombie_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, first_pack, _ = self._first_not_clear(root)
            state_path = StateStore(root).path("C001")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            zombie_relative = ".dls/reviews/C001/packs/zombie.json"
            zombie_path = root / zombie_relative
            zombie_path.write_text('{"schema_version": 1}', encoding="utf-8")
            state["reviews"].insert(
                0,
                {
                    "review_id": "zombie",
                    "kind": "pack",
                    "pack_path": zombie_relative,
                    "head_sha": first_pack["review_pack"]["head_sha"],
                },
            )
            state_path.write_text(
                json.dumps(state, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            current_head = self._advance_candidate(
                root,
                disposition_status="addressed",
            )

            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="auto-repeat",
            )
            replayed = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="auto-repeat",
            )

            self.assertTrue(started["ok"])
            self.assertTrue(started["pack_created"])
            self.assertEqual(started["review_mode"], "remediation")
            self.assertEqual(
                [item["finding_id"] for item in started["required_prior_findings"]],
                ["R001"],
            )
            self.assertEqual(
                StateStore(root).load("C001")["reviews"][-2]["head_sha"],
                current_head,
            )
            context = json.loads(
                (root / started["review_context_path"]).read_text(encoding="utf-8")
            )
            context_paths = {item["path"] for item in context["inputs"]}
            self.assertIn(started["review_pack_path"], context_paths)
            self.assertNotIn(zombie_relative, context_paths)
            self.assertEqual(replayed["review_id"], started["review_id"])
            self.assertFalse(replayed["pack_created"])
            self.assertTrue(replayed["native_reused"])

    def test_latest_review_is_the_only_canonical_finding_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_relative = ".dls/reviews/C001/results/old.json"
            current_relative = ".dls/reviews/C001/results/current.json"
            old_path = root / old_relative
            current_path = root / current_relative
            old_path.parent.mkdir(parents=True)
            old_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "findings": [
                            {
                                "id": "R-OLD",
                                "severity": "blocker",
                                "kind": "defect",
                                "blocks": ["review", "acceptance"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            current_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "findings": [
                            {
                                "id": "R-CURRENT",
                                "severity": "should-fix",
                                "kind": "validation-gap",
                                "blocks": ["review", "acceptance"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            state = {
                "reviews": [
                    {"kind": "result", "result_path": old_relative},
                    {"kind": "result", "result_path": current_relative},
                ],
                "finding_dispositions": [],
            }

            self.assertEqual(
                set(_all_review_findings(root, state)),
                {"R-OLD", "R-CURRENT"},
            )
            self.assertEqual(
                set(_canonical_review_findings(root, state)),
                {"R-CURRENT"},
            )
            self.assertEqual(
                [item["finding_id"] for item in _active_prior_findings(root, state)],
                ["R-CURRENT"],
            )
            self.assertEqual(
                _open_finding_counts(root, state),
                {"blocker": 0, "should-fix": 1, "note": 0},
            )

    def test_note_requires_independent_release_only_reclassification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._first_not_clear(root)
            self._advance_candidate(root, disposition_status="note")
            ready = review_ready(
                root,
                change_id="C001",
                base_ref=None,
                expected_revision=8,
                operation_id="ready-note",
            )
            self.assertTrue(ready["ok"])
            prior = ready["review_pack"]["required_prior_findings"]
            self.assertEqual(prior[0]["disposition"]["status"], "note")

            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="start-note",
            )
            pack = ready["review_pack"]
            release_only = {
                "id": "R002",
                "severity": "should-fix",
                "kind": "external",
                "location": "release evidence",
                "issue": "Production evidence remains open.",
                "impact": "Release readiness remains open.",
                "required_fix": "Capture the evidence before release.",
                "ticket_ids": [],
                "requirement_ids": [],
                "base_sha": pack["base_sha"],
                "head_sha": pack["head_sha"],
                "blocks": ["release", "production"],
            }
            report = build_review_report(
                root,
                pack_result=ready,
                start_result=started,
                verdict="review-clear",
                findings=[release_only],
                prior_finding_verdicts=[
                    {
                        "finding_id": "R001",
                        "verdict": "verified",
                        "evidence": [
                            "The prior review-stage classification does not apply."
                        ],
                    }
                ],
            )
            report_path = root / ".dls/cache/reclassified.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            imported = review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/reclassified.json",
                expected_revision=10,
                operation_id="import-reclassified",
            )

            self.assertEqual(imported["verdict"], "review-clear")
            self.assertEqual(
                set(_canonical_review_findings(root, StateStore(root).load("C001"))),
                {"R002"},
            )
            self.assertEqual(
                _open_finding_counts(root, StateStore(root).load("C001")),
                {"blocker": 0, "should-fix": 0, "note": 0},
            )

    def test_reopened_finding_still_blocks_review_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._first_not_clear(root)
            self._advance_candidate(root, disposition_status="reopened")

            blocked = review_ready(
                root,
                change_id="C001",
                base_ref=None,
                expected_revision=8,
                operation_id="ready-reopened",
                dry_run=True,
            )

            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["next_action"]["id"], "address-review-findings")
            self.assertIn("R001", blocked["next_action"]["detail"])


if __name__ == "__main__":
    unittest.main()
