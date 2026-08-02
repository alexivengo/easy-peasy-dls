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


if __name__ == "__main__":
    unittest.main()
