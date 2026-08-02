from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


class M2EvaluationDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        repository_root = Path(__file__).resolve().parents[3]
        validator_path = repository_root / "scripts" / "validate_public_repo.py"
        spec = importlib.util.spec_from_file_location("m2_public_validator", validator_path)
        assert spec and spec.loader
        self.validator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.validator)
        source_docs = repository_root / "docs"
        self.cases = self.root / "evaluation-m2-cases.md"
        self.runbook = self.root / "evaluation-m2-runbook.md"
        self.decisions = self.root / "evaluation-m2-decisions.md"
        for destination in (self.cases, self.runbook, self.decisions):
            destination.write_text(
                (source_docs / destination.name).read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        self.original_paths = (
            self.validator.M2_CASES_DOCUMENT,
            self.validator.M2_RUNBOOK_DOCUMENT,
            self.validator.M2_DECISIONS_DOCUMENT,
        )
        self.validator.M2_CASES_DOCUMENT = self.cases
        self.validator.M2_RUNBOOK_DOCUMENT = self.runbook
        self.validator.M2_DECISIONS_DOCUMENT = self.decisions

    def tearDown(self) -> None:
        (
            self.validator.M2_CASES_DOCUMENT,
            self.validator.M2_RUNBOOK_DOCUMENT,
            self.validator.M2_DECISIONS_DOCUMENT,
        ) = self.original_paths
        self.temporary.cleanup()

    def test_planned_documents_and_privacy_markers(self) -> None:
        self.validator.validate_m2_evaluation_documents()
        markers = (
            "| marker | file:// |",
            "```",
            "# prompt",
            "| marker | session_id=1 |",
            "| marker | " + "sk-" + "a" * 20 + " |",
            "| marker | private-fixture |",
            "diff --git a/case b/case",
        )
        source = self.cases.read_text(encoding="utf-8")
        for marker in markers:
            self.cases.write_text(source + marker + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "m2-privacy"):
                self.validator.validate_m2_evaluation_documents()
        self.cases.write_text(source, encoding="utf-8")

    def test_schema_rejects_missing_field_and_invalid_planned_exception(self) -> None:
        source_cases = self.cases.read_text(encoding="utf-8")
        self.cases.write_text(source_cases.replace("| oracle_owner |", "| unknown_owner |", 1), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "m2-field-shape"):
            self.validator.validate_m2_evaluation_documents()
        self.cases.write_text(source_cases, encoding="utf-8")
        source_decisions = self.decisions.read_text(encoding="utf-8")
        self.decisions.write_text(
            source_decisions.replace(
                "| reference_manifest_digest | not-applicable |",
                "| reference_manifest_digest | not-locked |",
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "m2-state-transition"):
            self.validator.validate_m2_evaluation_documents()
        self.decisions.write_text(source_decisions, encoding="utf-8")
        self.decisions.write_text(
            source_decisions.replace(
                "| repair_execution_proof_digest | not-applicable |",
                "| repair_execution_proof_digest | not-run |",
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "m2-state-transition"):
            self.validator.validate_m2_evaluation_documents()
        self.decisions.write_text(source_decisions, encoding="utf-8")
        self.decisions.write_text(
            source_decisions.replace("| plugin_version | not-locked |", "| plugin_version | dls 0.13.6+codex.20260802111333 |", 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "m2-state-transition"):
            self.validator.validate_m2_evaluation_documents()

    def test_clear_requires_each_arm_receipt(self) -> None:
        receipt = "sha256:" + "a" * 64

        def fields(proof: str = "not-applicable") -> dict[str, str]:
            return {
                "processed_tokens": "1",
                "wall_time_seconds": "1",
                "repair_execution_proof_digest": proof,
            }

        def row(
            arm_id: str,
            verdict: str,
            outcome: str,
            safety: str,
            finding: str,
        ) -> list[str]:
            return [
                arm_id,
                "",
                "",
                "",
                "",
                "",
                verdict,
                outcome,
                "passed",
                safety,
                "primary",
                "not-run",
                "not-run",
                finding,
                receipt,
            ]

        records = {
            "SR-01": (fields(), {"SR-01.current": row("SR-01.current", "review-clear", "passed", "0", "useful")}),
            "SR-02": (fields(), {"SR-02.current": row("SR-02.current", "not-clear", "passed", "0", "no-finding")}),
            "SR-03": (
                fields(),
                {
                    "SR-03.current": row("SR-03.current", "not-clear", "passed", "0", "no-finding"),
                    "SR-03.primary-only": row("SR-03.primary-only", "review-clear", "passed", "not-applicable", "dangerous-miss"),
                },
            ),
            "SR-04": (
                fields(receipt),
                {
                    "SR-04.repair": row("SR-04.repair", "review-clear", "passed", "0", "no-finding"),
                    "SR-04.fail-closed": row("SR-04.fail-closed", "not-applicable", "invalid-case", "not-applicable", "not-applicable"),
                },
            ),
        }
        self.validator._m2_validate_clear(records, "SR-01.current:useful")
        records["SR-03"][1]["SR-03.current"][14] = "not-run"
        with self.assertRaisesRegex(ValueError, "m2-overall-outcome"):
            self.validator._m2_validate_clear(records, "SR-01.current:useful")

    def test_executed_arm_requires_receipt(self) -> None:
        arm = self.validator.M2_ARMS["SR-01"][0]
        row = list(arm) + [
            "review-clear",
            "passed",
            "passed",
            "0",
            "primary",
            "primary=1;secondary=0;repair=0;transport-failed=0",
            "primary=1;secondary=0;repair=0",
            "no-finding",
            "not-run",
        ]
        with self.assertRaisesRegex(ValueError, "m2-receipt"):
            self.validator._m2_validate_actual("SR-01", arm, row, "completed")
        row[-1] = "sha256:" + "a" * 64
        self.validator._m2_validate_actual("SR-01", arm, row, "completed")


if __name__ == "__main__":
    unittest.main()
