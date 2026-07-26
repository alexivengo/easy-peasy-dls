from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dls_core.review_presentation import (
    PRESENTATION_CONTRACT,
    build_review_presentation,
)

from support import git, initialize_git


class ReviewPresentationV032Tests(unittest.TestCase):
    def _report(self, root: Path) -> dict:
        return {
            "review_id": "review-1",
            "head_sha": git(root, "rev-parse", "HEAD"),
            "findings": [
                {
                    "id": "R001",
                    "severity": "blocker",
                    "location": "Sources/App.swift:2-12,1; README.md:1",
                    "issue": 'A value "escapes"\nonto another line.',
                    "impact": "The request can fail.",
                    "required_fix": "Keep the value bounded.",
                },
                {
                    "id": "R002",
                    "severity": "should-fix",
                    "location": "../outside.swift:1",
                    "issue": "Unsafe location.",
                    "impact": "Must not escape the owner.",
                    "required_fix": "Use an owner path.",
                },
                {
                    "id": "R003",
                    "severity": "note",
                    "location": "README.md:1",
                    "issue": "Informational only.",
                    "impact": "None.",
                    "required_fix": "None.",
                },
            ],
        }

    def test_builds_safe_exact_head_inline_comment_directives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            source = root / "Sources" / "App.swift"
            source.parent.mkdir()
            source.write_text(
                "\n".join(f"line {index}" for index in range(1, 13)) + "\n",
                encoding="utf-8",
            )
            git(root, "add", "Sources/App.swift")
            git(root, "commit", "-m", "add source")

            presentation = build_review_presentation(root, self._report(root))

            self.assertEqual(presentation["contract"], PRESENTATION_CONTRACT)
            self.assertTrue(presentation["exact_head"])
            self.assertTrue(presentation["renderable"])
            self.assertFalse(presentation["all_actionable_placed"])
            self.assertEqual(presentation["errors"], [])
            self.assertEqual(len(presentation["comments"]), 1)
            comment = presentation["comments"][0]
            self.assertEqual(comment["finding_id"], "R001")
            self.assertEqual(comment["priority"], 1)
            self.assertEqual(comment["repository_path"], "Sources/App.swift")
            self.assertEqual((comment["start"], comment["end"]), (2, 2))
            self.assertEqual(comment["source_end"], 12)
            self.assertEqual(
                comment["related_locations"][0]["repository_path"],
                "Sources/App.swift",
            )
            self.assertEqual(
                (
                    comment["related_locations"][0]["start"],
                    comment["related_locations"][0]["end"],
                ),
                (1, 1),
            )
            self.assertEqual(
                comment["related_locations"][1]["repository_path"],
                "README.md",
            )
            self.assertIn("::code-comment{", comment["directive"])
            self.assertIn('\\"escapes\\"', comment["directive"])
            self.assertNotIn("\n", comment["directive"])
            self.assertEqual(
                presentation["unplaced_findings"],
                [
                    {
                        "finding_id": "R002",
                        "location": "../outside.swift:1",
                        "reason": "no-safe-owner-location",
                    }
                ],
            )

    def test_stale_head_never_emits_inline_directives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            source = root / "Sources" / "App.swift"
            source.parent.mkdir()
            source.write_text("one\ntwo\nthree\n", encoding="utf-8")
            git(root, "add", "Sources/App.swift")
            git(root, "commit", "-m", "add source")
            report = self._report(root)
            (root / "README.md").write_text("# Changed\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "advance head")

            presentation = build_review_presentation(root, report)

            self.assertFalse(presentation["exact_head"])
            self.assertFalse(presentation["renderable"])
            self.assertFalse(presentation["all_actionable_placed"])
            self.assertEqual(presentation["comments"], [])
            self.assertEqual(
                [item["finding_id"] for item in presentation["unplaced_findings"]],
                ["R001", "R002"],
            )

    def test_malformed_legacy_presentation_never_breaks_review_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            presentation = build_review_presentation(
                root,
                {
                    "review_id": "legacy",
                    "head_sha": git(root, "rev-parse", "HEAD"),
                    "findings": ["invalid", {"severity": "blocker"}],
                },
            )

            self.assertEqual(presentation["comments"], [])
            self.assertEqual(
                presentation["errors"],
                [
                    "review-finding-is-not-an-object",
                    "actionable-finding-has-no-id",
                ],
            )


if __name__ == "__main__":
    unittest.main()
