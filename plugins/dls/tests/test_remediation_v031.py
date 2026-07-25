from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dls_core.errors import IntegrityError
from dls_core.operations import (
    evidence_add,
    finding_disposition,
    remediation_recover,
    remediation_start,
    review_import,
    review_ready,
)
from dls_core.review_runner import review_status
from dls_core.state import StateStore

from support import git
import test_review_loop_v13 as review_loop


class RemediationV031Tests(unittest.TestCase):
    def _legacy_gap(self, root: Path) -> tuple[str, str, str]:
        helper = review_loop.ReviewLoopV13Tests()
        base_sha, _, _ = helper._first_not_clear(root)
        state_path = StateStore(root).path("C001")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        result_entry = next(
            item
            for item in reversed(state["reviews"])
            if item.get("kind") == "result"
        )
        review_id = result_entry["review_id"]
        reviewed_head = result_entry["head_sha"]
        manifest_path = root / result_entry.pop("remediation_manifest_path")
        result_entry.pop("remediation_manifest_digest")
        manifest_path.unlink()
        state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return base_sha, review_id, reviewed_head

    def test_not_clear_import_atomically_links_canonical_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            review_loop.ReviewLoopV13Tests()._first_not_clear(root)
            state = StateStore(root).load("C001")
            result_entry = next(
                item
                for item in reversed(state["reviews"])
                if item.get("kind") == "result"
            )
            relative = result_entry["remediation_manifest_path"]
            self.assertIn(".dls/reviews/C001/remediations/", relative)
            manifest = json.loads((root / relative).read_text(encoding="utf-8"))
            self.assertEqual(manifest["origin"], "review-import")
            self.assertEqual(
                result_entry["remediation_manifest_digest"],
                manifest["manifest_digest"],
            )
            self.assertEqual(
                [item["finding_id"] for item in manifest["open_findings"]],
                ["R001"],
            )
            replayed = review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/review-one.json",
                expected_revision=state["state_revision"],
                operation_id="import-replay",
            )
            self.assertFalse(replayed["changed"])
            self.assertEqual(replayed["remediation_manifest_path"], relative)
            self.assertEqual(
                len(
                    [
                        item
                        for item in StateStore(root).load("C001")["reviews"]
                        if item.get("kind") == "result"
                    ]
                ),
                1,
            )
            cache = root / ".dls/cache"
            for path in sorted(cache.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            verified = remediation_start(root, change_id="C001")
            self.assertEqual(verified["remediation_manifest_path"], relative)

    def test_legacy_recovery_uses_reviewed_git_tree_without_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha, review_id, reviewed_head = self._legacy_gap(root)
            (root / "README.md").write_text(
                "# Fixture\n\nFirst remediation commit.\n",
                encoding="utf-8",
            )
            git(root, "add", "README.md")
            git(root, "commit", "-m", "first remediation commit")
            (root / "README.md").write_text(
                "# Fixture\n\nSecond remediation commit.\n",
                encoding="utf-8",
            )
            git(root, "add", "README.md")
            git(root, "commit", "-m", "second remediation commit")
            current_head = git(root, "rev-parse", "HEAD")
            evidence = evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=0,
                summary="PASS recovered candidate",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                git_sha=current_head,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="recovery-evidence",
            )
            finding_disposition(
                root,
                change_id="C001",
                finding_id="R001",
                disposition_status="addressed",
                rationale="Fixed across the two remediation commits.",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                git_sha=current_head,
                evidence=[evidence["evidence_path"]],
                actor="codex",
                prompt=None,
                response=None,
                operation_id="recovery-addressed",
            )
            before_head = git(root, "rev-parse", "HEAD")
            before_source = git(root, "diff", "--", "README.md")
            status = review_status(root, change_id="C001")
            self.assertEqual(
                status["next_action"]["id"],
                "recover-remediation-manifest",
            )
            dry_run = remediation_recover(
                root,
                change_id="C001",
                review_id=review_id,
                operation_id="recover-review",
                dry_run=True,
            )
            projected = dry_run["projected_remediation_manifest_path"]
            self.assertFalse((root / projected).exists())
            recovered = remediation_recover(
                root,
                change_id="C001",
                review_id=review_id,
                operation_id="recover-review",
            )
            self.assertEqual(recovered["reviewed_head"], reviewed_head)
            self.assertEqual(recovered["current_head"], current_head)
            self.assertTrue((root / recovered["remediation_manifest_path"]).is_file())
            self.assertEqual(git(root, "rev-parse", "HEAD"), before_head)
            self.assertEqual(git(root, "diff", "--", "README.md"), before_source)
            ready = review_ready(
                root,
                change_id="C001",
                base_ref=base_sha,
                expected_revision=StateStore(root).load("C001")["state_revision"],
                operation_id="ready-after-recovery",
            )
            self.assertTrue(ready["ok"])
            self.assertTrue(ready["handoff_required"])
            self.assertEqual(ready["next_action"]["id"], "open-review-task")

    def test_recovery_rejects_dirty_definition_drift_and_tampered_pack(self) -> None:
        with self.subTest("dirty product source"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, review_id, _ = self._legacy_gap(root)
                (root / "README.md").write_text("# dirty\n", encoding="utf-8")
                with self.assertRaisesRegex(IntegrityError, "clean product source"):
                    remediation_recover(
                        root,
                        change_id="C001",
                        review_id=review_id,
                        operation_id="recover-dirty",
                    )

        with self.subTest("definition drift"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, review_id, _ = self._legacy_gap(root)
                spec = next((root / "docs").rglob("SPEC.md"))
                spec.write_text(
                    spec.read_text(encoding="utf-8") + "\nChanged behavior.\n",
                    encoding="utf-8",
                )
                git(root, "add", str(spec.relative_to(root)))
                git(root, "commit", "-m", "change definition")
                with self.assertRaisesRegex(IntegrityError, "definition digest"):
                    remediation_recover(
                        root,
                        change_id="C001",
                        review_id=review_id,
                        operation_id="recover-definition-drift",
                    )

        with self.subTest("tampered pack"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _, review_id, _ = self._legacy_gap(root)
                state = StateStore(root).load("C001")
                pack_entry = next(
                    item
                    for item in state["reviews"]
                    if item.get("kind") == "pack"
                    and item.get("review_id") == review_id
                )
                pack_path = root / pack_entry["pack_path"]
                pack = json.loads(pack_path.read_text(encoding="utf-8"))
                pack["changed_files"].append("tampered")
                pack_path.write_text(json.dumps(pack), encoding="utf-8")
                with self.assertRaisesRegex(IntegrityError, "digest"):
                    remediation_recover(
                        root,
                        change_id="C001",
                        review_id=review_id,
                        operation_id="recover-tampered-pack",
                    )
