from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from dls_core.cli import build_parser, dispatch, main
from dls_core.candidate_runner import candidate_ready
from dls_core.delivery_receipt import (
    RECEIPT_CONTRACT,
    RECEIPT_ITEM_LIMIT,
    delivery_receipt,
)
from dls_core.operations import approve, evidence_add, review_import, review_pack
from dls_core.state import StateStore

from support import (
    build_review_report,
    create_change,
    git,
    initialize,
    initialize_git,
    start_review_with_fake_codex,
)


class DeliveryReceiptV080Tests(unittest.TestCase):
    def _inventory(self, root: Path) -> dict[str, str]:
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file() and ".git" not in path.parts
        }

    def _reviewed_fixture(
        self,
        root: Path,
        *,
        verdict: str,
        findings: list[dict] | None = None,
        use_dispatch: bool = False,
    ) -> tuple[str, dict]:
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
        normalized_findings = []
        for finding in findings or []:
            item = dict(finding)
            item.setdefault("base_sha", pack["review_pack"]["base_sha"])
            item.setdefault("head_sha", pack["review_pack"]["head_sha"])
            normalized_findings.append(item)
        report = build_review_report(
            root,
            pack_result=pack,
            start_result=started,
            verdict=verdict,
            findings=normalized_findings,
        )
        report_path = root / ".dls/cache/review.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report), encoding="utf-8")
        revision = StateStore(root).load("C001")["state_revision"]
        if use_dispatch:
            arguments = build_parser().parse_args(
                [
                    "review-import",
                    "C001",
                    ".dls/cache/review.json",
                    "--expect-revision",
                    str(revision),
                    "--operation-id",
                    "review-import-1",
                ]
            )
            imported = dispatch(root, arguments)
        else:
            imported = review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/review.json",
                expected_revision=revision,
                operation_id="review-import-1",
            )
        return head_sha, imported

    def test_draft_receipt_is_deterministic_read_only_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="routine")
            before = self._inventory(root)

            with (
                mock.patch("dls_core.operations._run_bounded_command") as model_runner,
                mock.patch.dict(
                    os.environ,
                    {"CODEX_THREAD_ID": "019f-receipt-must-not-bind-telemetry"},
                ),
            ):
                first = delivery_receipt(root, change_id="C001")
                second = delivery_receipt(root, change_id="C001")
            model_runner.assert_not_called()

            self.assertEqual(first, second)
            self.assertEqual(self._inventory(root), before)
            self.assertEqual(first["contract"], RECEIPT_CONTRACT)
            self.assertEqual(first["lifecycle"], "source-dirty")
            self.assertEqual(first["definition"]["status"], "pending")
            self.assertIsNotNone(first["outcome"])
            structured = {
                key: value
                for key, value in first.items()
                if key not in {"receipt_digest", "markdown_digest", "markdown"}
            }
            self.assertEqual(
                first["receipt_digest"],
                hashlib.sha256(
                    json.dumps(
                        structured,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            )
            self.assertEqual(
                first["markdown_digest"],
                hashlib.sha256(first["markdown"].encode("utf-8")).hexdigest(),
            )
            self.assertLessEqual(len(first["markdown"].encode("utf-8")), 4096)
            self.assertLessEqual(
                len(json.dumps(first, indent=2, ensure_ascii=False).encode("utf-8")),
                16 * 1024,
            )

    def test_latest_review_is_current_then_stale_without_ghost_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding = {
                "id": "R001",
                "severity": "should-fix",
                "kind": "defect",
                "location": "README.md:1",
                "issue": "A defect remains.",
                "impact": "Review cannot clear.",
                "required_fix": "Fix it.",
                "blocks": ["review", "acceptance"],
            }
            reviewed_head, imported = self._reviewed_fixture(
                root,
                verdict="not-clear",
                findings=[finding],
            )
            self.assertEqual(
                imported["delivery_receipt"]["lifecycle"],
                "not-clear",
            )
            historical_report = {
                "schema_version": 1,
                "review_id": "historical-review",
                "change_id": "C001",
                "head_sha": reviewed_head,
                "verdict": "not-clear",
                "findings": [
                    {
                        "id": "R-HISTORICAL",
                        "severity": "blocker",
                        "kind": "defect",
                    }
                ],
            }
            historical_path = root / ".dls/reviews/C001/results/historical-review.json"
            historical_path.write_text(
                json.dumps(historical_report, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            state_path = root / ".dls/state/C001.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            latest_index = max(
                index
                for index, item in enumerate(state["reviews"])
                if item.get("kind") == "result"
            )
            state["reviews"].insert(
                latest_index,
                {
                    "review_id": "historical-review",
                    "kind": "result",
                    "result_path": ".dls/reviews/C001/results/historical-review.json",
                    "head_sha": reviewed_head,
                    "verdict": "not-clear",
                    "result_digest": hashlib.sha256(
                        json.dumps(
                            historical_report,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                },
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")

            current = delivery_receipt(root, change_id="C001")
            self.assertEqual(current["lifecycle"], "not-clear")
            self.assertTrue(current["review"]["current"])
            self.assertEqual(
                current["review"]["current_finding_counts"]["should-fix"], 1
            )
            self.assertEqual(current["review"]["historical"]["review_count"], 1)
            self.assertEqual(
                current["review"]["historical"]["finding_counts"]["blocker"], 1
            )
            self.assertEqual(current["release"]["status"], "not-evaluated")

            (root / "README.md").write_text("# New HEAD\n", encoding="utf-8")
            dirty = delivery_receipt(root, change_id="C001")
            self.assertFalse(dirty["source_clean"])
            self.assertEqual(dirty["lifecycle"], "source-dirty")
            self.assertEqual(dirty["review"]["status"], "stale")
            self.assertNotEqual(dirty["receipt_digest"], current["receipt_digest"])
            git(root, "add", "README.md")
            git(root, "commit", "-m", "advance head")
            stale = delivery_receipt(root, change_id="C001")
            self.assertNotEqual(stale["head_sha"], reviewed_head)
            self.assertEqual(stale["review"]["status"], "stale")
            self.assertFalse(stale["review"]["current"])
            self.assertEqual(
                stale["review"]["current_finding_counts"]["should-fix"], 0
            )
            self.assertEqual(
                stale["review"]["latest_finding_counts"]["should-fix"], 1
            )
            self.assertNotEqual(stale["receipt_digest"], dirty["receipt_digest"])

    def test_release_only_finding_never_implies_release_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding = {
                "id": "R-RELEASE",
                "severity": "should-fix",
                "kind": "external",
                "location": "docs/release.md:1",
                "issue": "External proof is pending.",
                "impact": "Release evidence is incomplete.",
                "required_fix": "Collect release evidence.",
                "blocks": ["release", "production"],
            }
            self._reviewed_fixture(root, verdict="review-clear", findings=[finding])
            receipt = delivery_receipt(root, change_id="C001")

            self.assertEqual(receipt["lifecycle"], "review-clear")
            self.assertEqual(receipt["release"]["status"], "blocked")
            self.assertEqual(receipt["production"]["status"], "blocked")
            self.assertEqual(
                receipt["review"]["current_finding_counts"]["should-fix"], 1
            )

    def test_approved_candidate_ready_uses_exact_head_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha = initialize_git(root)
            initialize(root)
            create_change(root, control="standard")
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "[policy]\n",
                    '[policy]\nreview_required_commands = ["test"]\n',
                    1,
                )
                + f"""

[commands.test]
argv = ["{sys.executable}", "-c", "print('trusted pass')"]
cwd = "."
timeout_seconds = 5
max_output_bytes = 4096
env_allow = []
""",
                encoding="utf-8",
            )
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
            prepared = candidate_ready(
                root,
                change_id="C001",
                base_ref=base_sha,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id="candidate-1",
            )
            self.assertEqual(prepared["next_action"]["id"], "open-review-task")

            receipt = delivery_receipt(root, change_id="C001")
            self.assertEqual(receipt["lifecycle"], "candidate-ready")
            self.assertTrue(receipt["implementation"]["prepared"])
            self.assertTrue(receipt["implementation"]["exact_head"])
            self.assertEqual(receipt["validation"]["status"], "passing")
            self.assertEqual(receipt["validation"]["passing_count"], 1)
            evidence = receipt["validation"]["passing_evidence"]["items"][0]
            self.assertNotIn("trusted pass", json.dumps(evidence))
            self.assertTrue(evidence["evidence_path"].startswith(".dls/evidence/"))

    def test_legacy_blocked_review_is_readable(self) -> None:
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
            git(root, "commit", "-m", "legacy reviewed head")
            head_sha = git(root, "rev-parse", "HEAD")
            report = {
                "schema_version": 1,
                "review_id": "legacy-blocked",
                "change_id": "C001",
                "head_sha": head_sha,
                "verdict": "blocked",
                "findings": [],
            }
            result_path = root / ".dls/reviews/C001/results/legacy-blocked.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps(report), encoding="utf-8")
            state_path = root / ".dls/state/C001.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["reviews"].append(
                {
                    "kind": "result",
                    "review_id": "legacy-blocked",
                    "head_sha": head_sha,
                    "verdict": "blocked",
                    "result_path": ".dls/reviews/C001/results/legacy-blocked.json",
                    "result_digest": hashlib.sha256(
                        json.dumps(
                            report,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                }
            )
            state_path.write_text(json.dumps(state), encoding="utf-8")

            receipt = delivery_receipt(root, change_id="C001")
            self.assertEqual(receipt["lifecycle"], "blocked")
            self.assertEqual(receipt["review"]["status"], "blocked")

    def test_direct_import_and_accept_return_updated_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            head_sha, imported = self._reviewed_fixture(
                root,
                verdict="review-clear",
                use_dispatch=True,
            )
            self.assertEqual(
                imported["delivery_receipt"]["lifecycle"], "review-clear"
            )

            revision = StateStore(root).load("C001")["state_revision"]
            arguments = build_parser().parse_args(
                [
                    "approve",
                    "C001",
                    "--decision",
                    "accept",
                    "--actor",
                    "user",
                    "--git-sha",
                    head_sha,
                    "--expect-revision",
                    str(revision),
                    "--operation-id",
                    "accept-1",
                ]
            )
            accepted = dispatch(root, arguments)
            receipt = accepted["delivery_receipt"]
            self.assertEqual(receipt["lifecycle"], "accepted")
            self.assertEqual(receipt["acceptance"]["status"], "accepted")
            self.assertEqual(receipt["release"]["status"], "not-evaluated")
            self.assertEqual(receipt["production"]["status"], "not-evaluated")
            self.assertEqual(receipt["next_action"]["id"], "delivery-complete")

            (root / "README.md").write_text("# Changed after acceptance\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "advance accepted head")
            stale = delivery_receipt(root, change_id="C001")
            self.assertEqual(stale["acceptance"]["status"], "stale")
            self.assertNotEqual(stale["lifecycle"], "accepted")

    def test_narrative_redacts_secrets_paths_and_caps_arrays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="routine")
            state_path = root / ".dls/state/C001.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["tickets"] = {
                f"T{index:02d}": {"status": "planned"}
                for index in range(RECEIPT_ITEM_LIMIT + 5)
            }
            state_path.write_text(json.dumps(state), encoding="utf-8")
            change_path = root / state["artifacts"]["change"]["path"]
            user_path = "/" + "Users/private"
            change_path.write_text(
                f"# Secret token=very-secret {user_path}/project $HOME/private ~/local\n\n"
                "## Outcome\n\nShip password=hunter2 from /tmp/private/output.\n",
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {"CODEX_THREAD_ID": "019f-secret-thread-id"},
            ):
                receipt = delivery_receipt(root, change_id="C001")
            encoded = json.dumps(receipt, ensure_ascii=False)
            self.assertNotIn("very-secret", encoded)
            self.assertNotIn("hunter2", encoded)
            self.assertNotIn(user_path, encoded)
            self.assertNotIn("/tmp/private", encoded)
            self.assertNotIn("$HOME/private", encoded)
            self.assertNotIn("~/local", encoded)
            self.assertNotIn("019f-secret-thread-id", encoded)
            self.assertEqual(
                len(receipt["implementation"]["tickets"]["items"]),
                RECEIPT_ITEM_LIMIT,
            )
            self.assertEqual(
                receipt["implementation"]["tickets"]["omitted_count"], 5
            )

    def test_missing_outcome_uses_no_invented_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            spec = root / "docs/existing/SPEC.md"
            spec.parent.mkdir(parents=True)
            spec.write_text("No H1 and no outcome section.\n", encoding="utf-8")
            arguments = build_parser().parse_args(
                [
                    "adopt",
                    "E01",
                    "--slug",
                    "existing-contract",
                    "--kind",
                    "feature",
                    "--control",
                    "standard",
                    "--artifact",
                    "spec=docs/existing/SPEC.md",
                    "--operation-id",
                    "adopt-e01",
                ]
            )
            dispatch(root, arguments)

            receipt = delivery_receipt(root, change_id="E01")
            self.assertEqual(receipt["title"], "E01 — existing-contract")
            self.assertIsNone(receipt["outcome"])

    def test_public_cli_renders_markdown_or_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize_git(root)
            initialize(root)
            create_change(root, control="routine")

            json_output = io.StringIO()
            with redirect_stdout(json_output):
                exit_code = main(
                    ["--root", str(root), "--json", "delivery-receipt", "C001"]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(json_output.getvalue())
            self.assertEqual(payload["contract"], RECEIPT_CONTRACT)
            self.assertIn("dls_version", payload)

            markdown_output = io.StringIO()
            with redirect_stdout(markdown_output):
                exit_code = main(
                    ["--root", str(root), "delivery-receipt", "C001"]
                )
            self.assertEqual(exit_code, 0)
            self.assertEqual(markdown_output.getvalue(), payload["markdown"] + "\n")


if __name__ == "__main__":
    unittest.main()
