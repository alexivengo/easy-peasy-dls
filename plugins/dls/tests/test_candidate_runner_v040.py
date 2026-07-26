from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dls_core.candidate_runner import candidate_ready, candidate_status
from dls_core.errors import IntegrityError, UsageError
from dls_core.operations import approve, review_import
from dls_core.state import StateStore
from dls_core.worktrees import worktree_register

from support import (
    build_review_report,
    create_change,
    git,
    initialize,
    initialize_git,
    start_review_with_fake_codex,
)

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CLI = PLUGIN_ROOT / "scripts" / "dls.py"


class CandidateRunnerV040Tests(unittest.TestCase):
    def _initial_candidate(self, root: Path) -> str:
        base = initialize_git(root)
        initialize(root)
        create_change(root, control="standard")
        config = root / ".dls/config.toml"
        config_text = config.read_text(encoding="utf-8").replace(
            "[policy]\n",
            '[policy]\nreview_required_commands = ["test", "bridge"]\n',
            1,
        )
        config.write_text(
            config_text
            + f"""

[commands.test]
argv = ["{sys.executable}", "-c", "print('75 tests passed')"]
cwd = "."
timeout_seconds = 5
max_output_bytes = 4096
env_allow = []

[commands.bridge]
argv = ["{sys.executable}", "-c", "print('bridge passed')"]
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
            operation_id="approve-definition",
        )
        git(root, "add", ".dls", "docs")
        git(root, "commit", "-m", "implementation candidate")
        return base

    def test_initial_candidate_runs_required_commands_and_creates_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._initial_candidate(root)
            result = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["next_action"]["id"], "open-review-task")
            self.assertTrue((root / result["review_pack_path"]).is_file())
            state = StateStore(root).load("C001")
            run = state["candidate_runs"][-1]
            self.assertEqual(run["status"], "completed")
            self.assertEqual(
                [item["status"] for item in run["commands"]],
                ["completed", "completed"],
            )
            evidence = [
                json.loads((root / relative).read_text(encoding="utf-8"))
                for relative in state["evidence"]
            ]
            self.assertEqual(
                {record["command_id"] for record in evidence},
                {"test", "bridge"},
            )
            self.assertTrue(
                all("tests passed" not in record["summary"] for record in evidence)
            )
            status = candidate_status(root, change_id="C001")
            self.assertEqual(status["status"], "completed")
            self.assertEqual(status["next_action"]["id"], "open-review-task")
            self.assertLess(len(json.dumps(result).encode("utf-8")), 4096)
            self.assertLess(len(json.dumps(status).encode("utf-8")), 1024)
            replay = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id="different-operation-id",
            )
            self.assertEqual(replay["review_pack_path"], result["review_pack_path"])
            replayed_state = StateStore(root).load("C001")
            self.assertEqual(len(replayed_state["evidence"]), 2)
            self.assertEqual(
                len([item for item in replayed_state["reviews"] if item.get("kind") == "pack"]),
                1,
            )

    def test_candidate_cli_end_to_end_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._initial_candidate(root)
            execution = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--root",
                    str(root),
                    "--json",
                    "candidate-ready",
                    "C001",
                    "--base",
                    base,
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(execution.returncode, 0, execution.stderr or execution.stdout)
            result = json.loads(execution.stdout)
            self.assertEqual(result["status"], "completed")
            self.assertTrue((root / result["review_pack_path"]).is_file())

    def test_missing_review_commands_returns_typed_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = initialize_git(root)
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
                operation_id="approve-definition",
            )
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "candidate")
            result = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(
                result["next_action"]["id"],
                "configure-review-commands",
            )
            self.assertFalse(StateStore(root).load("C001").get("candidate_runs"))

    def test_candidate_dry_run_projects_without_commands_or_state_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._initial_candidate(root)
            before = StateStore(root).load("C001")
            result = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
                dry_run=True,
            )
            self.assertEqual(result["status"], "projected")
            self.assertEqual(result["commands"], ["test", "bridge"])
            after = StateStore(root).load("C001")
            self.assertEqual(after, before)
            self.assertFalse(list((root / ".dls/evidence/C001").glob("*.json")))

    def test_remediation_attaches_all_evidence_and_atomically_creates_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._initial_candidate(root)
            first = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            first_pack = json.loads(
                (root / first["review_pack_path"]).read_text(encoding="utf-8")
            )
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="first-native",
            )
            findings = [
                {
                    "id": finding_id,
                    "severity": "should-fix",
                    "kind": "defect",
                    "location": "README.md:1",
                    "issue": f"Issue {finding_id}",
                    "impact": "The candidate is not clear.",
                    "required_fix": f"Fix {finding_id}.",
                    "ticket_ids": [],
                    "requirement_ids": [],
                    "base_sha": first_pack["base_sha"],
                    "head_sha": first_pack["head_sha"],
                    "blocks": ["review", "acceptance"],
                }
                for finding_id in ("R001", "R002")
            ]
            report = build_review_report(
                root,
                pack_result={"review_pack": first_pack},
                start_result=started,
                verdict="not-clear",
                findings=findings,
            )
            report_path = root / ".dls/cache/review.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/review.json",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                operation_id="import-first-review",
            )
            (root / "README.md").write_text("# Fixed candidate\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "remediate findings")
            with self.assertRaisesRegex(UsageError, "every actionable finding"):
                candidate_ready(
                    root,
                    change_id="C001",
                    base_ref=None,
                    addressed=["R001"],
                    noted=[],
                    extra_commands=[],
                    operation_id=None,
                )
            result = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=["R001"],
                noted=["R002"],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(result["status"], "completed")
            state = StateStore(root).load("C001")
            dispositions = {
                item["finding_id"]: item
                for item in state["finding_dispositions"]
                if item["finding_id"] in {"R001", "R002"}
            }
            self.assertEqual(dispositions["R001"]["status"], "addressed")
            self.assertEqual(len(dispositions["R001"]["evidence"]), 2)
            self.assertEqual(dispositions["R002"]["status"], "note")
            self.assertEqual(dispositions["R002"]["evidence"], [])
            pack = json.loads(
                (root / result["review_pack_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(pack["review_mode"], "remediation")
            self.assertEqual(
                {item["finding_id"] for item in pack["required_prior_findings"]},
                {"R001", "R002"},
            )

    def test_validation_failure_is_bounded_and_creates_no_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._initial_candidate(root)
            config = root / ".dls/config.toml"
            text = config.read_text(encoding="utf-8").replace(
                "print('75 tests passed')",
                "import sys; print('x' * 10000); sys.exit(7)",
            )
            config.write_text(text, encoding="utf-8")
            result = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["next_action"]["id"], "fix-validation")
            self.assertLessEqual(len(result.get("failure_excerpt", "")), 4096)
            state = StateStore(root).load("C001")
            self.assertFalse(any(item.get("kind") == "pack" for item in state["reviews"]))
            self.assertEqual(state["finding_dispositions"], [])

    def test_concurrent_calls_execute_each_command_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._initial_candidate(root)
            counter = root / ".dls/cache/candidate-counter.txt"
            counter.parent.mkdir(parents=True, exist_ok=True)
            config = root / ".dls/config.toml"
            text = config.read_text(encoding="utf-8")
            text = text.replace(
                "print('75 tests passed')",
                (
                    "import pathlib,time; "
                    "pathlib.Path('.dls/cache/candidate-counter.txt').open('a').write('test'+chr(10)); "
                    "time.sleep(1)"
                ),
            ).replace(
                "print('bridge passed')",
                (
                    "import pathlib; "
                    "pathlib.Path('.dls/cache/candidate-counter.txt').open('a').write('bridge'+chr(10))"
                ),
            )
            config.write_text(text, encoding="utf-8")
            barrier = threading.Barrier(2)

            def run(operation_id: str) -> dict:
                barrier.wait()
                return candidate_ready(
                    root,
                    change_id="C001",
                    base_ref=base,
                    addressed=[],
                    noted=[],
                    extra_commands=[],
                    operation_id=operation_id,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(run, ["candidate-a", "candidate-b"]))
            self.assertIn("completed", {item["status"] for item in results}, results)
            self.assertTrue({item["status"] for item in results} <= {"running", "completed"})
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines(),
                ["test", "bridge"],
            )

    def test_extra_named_command_is_recorded_as_canonical_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._initial_candidate(root)
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + f"""

[commands.focused]
argv = ["{sys.executable}", "-c", "print('focused passed')"]
cwd = "."
timeout_seconds = 5
max_output_bytes = 4096
env_allow = []
""",
                encoding="utf-8",
            )
            result = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=["focused"],
                operation_id=None,
            )
            self.assertEqual(result["status"], "completed")
            state = StateStore(root).load("C001")
            self.assertEqual(
                {
                    json.loads((root / path).read_text(encoding="utf-8"))["command_id"]
                    for path in state["evidence"]
                },
                {"test", "bridge", "focused"},
            )

    def test_source_drift_during_validation_is_integrity_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._initial_candidate(root)
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "print('75 tests passed')",
                    "import pathlib; pathlib.Path('README.md').write_text('drift')",
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(IntegrityError, "source changed"):
                candidate_ready(
                    root,
                    change_id="C001",
                    base_ref=base,
                    addressed=[],
                    noted=[],
                    extra_commands=[],
                    operation_id=None,
                )
            state = StateStore(root).load("C001")
            self.assertEqual(state["candidate_runs"][-1]["status"], "failed")
            self.assertFalse(any(item.get("kind") == "pack" for item in state["reviews"]))

    def test_validation_spawn_failure_is_infrastructure_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = self._initial_candidate(root)
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    f'argv = ["{sys.executable}", "-c", "print(\'75 tests passed\')"]',
                    'argv = ["/definitely/missing/dls-command"]',
                    1,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(IntegrityError, "Unable to start"):
                candidate_ready(
                    root,
                    change_id="C001",
                    base_ref=base,
                    addressed=[],
                    noted=[],
                    extra_commands=[],
                    operation_id=None,
                )
            state = StateStore(root).load("C001")
            self.assertEqual(state["candidate_runs"][-1]["status"], "failed")
            self.assertFalse(any(item.get("kind") == "pack" for item in state["reviews"]))

    def test_candidate_routes_to_registered_owner_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            base_root = sandbox / "base"
            owner = sandbox / "owner"
            base_root.mkdir()
            base_sha = initialize_git(base_root)
            git(base_root, "worktree", "add", "-b", "codex/C001", str(owner))
            initialize(owner)
            create_change(owner, control="standard")
            config = owner / ".dls/config.toml"
            text = config.read_text(encoding="utf-8").replace(
                "[policy]\n",
                '[policy]\nreview_required_commands = ["test"]\n',
                1,
            )
            config.write_text(
                text
                + f"""

[commands.test]
argv = ["{sys.executable}", "-c", "print('pass')"]
cwd = "."
timeout_seconds = 5
max_output_bytes = 4096
env_allow = []
""",
                encoding="utf-8",
            )
            approve(
                owner,
                change_id="C001",
                decision="definition",
                expected_revision=1,
                actor="user",
                prompt=None,
                response=None,
                git_sha=None,
                conditions=None,
                operation_id="approve-definition",
            )
            git(owner, "add", ".dls", "docs")
            git(owner, "commit", "-m", "candidate")
            worktree_register(base_root, change_id="C001", owner_path=owner)
            result = candidate_ready(
                base_root,
                change_id="C001",
                base_ref=base_sha,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(result["owner_selection"], "registered-worktree")
            self.assertEqual(Path(result["owner_root"]), owner.resolve())


if __name__ == "__main__":
    unittest.main()
