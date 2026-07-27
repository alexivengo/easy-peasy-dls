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

    def _import_findings(
        self,
        root: Path,
        finding_ids: tuple[str, ...],
    ) -> tuple[str, dict]:
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
            for finding_id in finding_ids
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
        return base, first_pack

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

    def test_candidate_cli_continues_descendant_without_finding_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._import_findings(root, ("R001", "R002"))
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "print('75 tests passed')",
                    (
                        "import sys; "
                        "sys.exit(0 if 'gate-pass' in open('README.md').read() else 7)"
                    ),
                ),
                encoding="utf-8",
            )
            (root / "README.md").write_text("# Candidate A\n", encoding="utf-8")
            git(root, "add", ".dls/config.toml", "README.md")
            git(root, "commit", "-m", "candidate A")
            first = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--root",
                    str(root),
                    "--json",
                    "candidate-ready",
                    "C001",
                    "--address",
                    "R001,R002",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            self.assertEqual(json.loads(first.stdout)["status"], "blocked")

            (root / "README.md").write_text("# Candidate B gate-pass\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate B")
            second = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--root",
                    str(root),
                    "--json",
                    "candidate-ready",
                    "C001",
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(second.returncode, 0, second.stderr or second.stdout)
            result = json.loads(second.stdout)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["declaration_source"], "inherited")

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
            incomplete = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=["R001"],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(incomplete["status"], "blocked")
            self.assertEqual(
                incomplete["next_action"]["id"],
                "declare-finding-disposition",
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

    def test_failed_candidate_continues_on_descendant_without_redeclaring_findings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding_ids = tuple(f"R{index:03d}" for index in range(1, 9))
            self._import_findings(root, finding_ids)
            counter = root / ".dls/cache/candidate-counter.txt"
            config = root / ".dls/config.toml"
            text = config.read_text(encoding="utf-8").replace(
                "print('75 tests passed')",
                (
                    "import pathlib; "
                    "pathlib.Path('.dls/cache/candidate-counter.txt').parent.mkdir(parents=True,exist_ok=True); "
                    "pathlib.Path('.dls/cache/candidate-counter.txt').open('a').write('test'+chr(10))"
                ),
            ).replace(
                "print('bridge passed')",
                (
                    "import pathlib,sys; "
                    "pathlib.Path('.dls/cache/candidate-counter.txt').open('a').write('bridge'+chr(10)); "
                    "ok='gate-pass' in pathlib.Path('README.md').read_text(); "
                    "print('bridge gate failed' if not ok else 'bridge passed'); "
                    "sys.exit(0 if ok else 7)"
                ),
            )
            config.write_text(text, encoding="utf-8")
            (root / "README.md").write_text("# Candidate A\n", encoding="utf-8")
            git(root, "add", ".dls/config.toml", "README.md")
            git(root, "commit", "-m", "candidate A")

            first = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(first["status"], "blocked")
            first_run = StateStore(root).load("C001")["candidate_runs"][-1]
            default_status = candidate_status(root, change_id="C001")
            diagnostic = candidate_status(root, change_id="C001", diagnostic=True)
            self.assertNotIn("validation_failure", default_status)
            self.assertLess(len(json.dumps(default_status).encode("utf-8")), 1024)
            self.assertEqual(
                diagnostic["validation_failure"]["command_id"],
                "bridge",
            )
            self.assertIn("bridge gate failed", diagnostic["validation_failure"]["excerpt"])
            self.assertLess(len(json.dumps(diagnostic).encode("utf-8")), 6144)

            (root / "README.md").write_text("# Candidate B gate-pass\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate B")
            second = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(second["status"], "completed")
            self.assertEqual(second["declaration_source"], "inherited")
            state = StateStore(root).load("C001")
            second_run = state["candidate_runs"][-1]
            self.assertNotEqual(second_run["run_id"], first_run["run_id"])
            self.assertNotEqual(second_run["operation_id"], first_run["operation_id"])
            self.assertEqual(second_run["parent_run_id"], first_run["run_id"])
            self.assertEqual(second_run["finding_dispositions"], first_run["finding_dispositions"])
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines(),
                ["test", "bridge", "test", "bridge"],
            )
            pack = json.loads(
                (root / second["review_pack_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(pack["head_sha"], git(root, "rev-parse", "HEAD").strip())

    def test_exact_head_retry_reuses_operation_and_only_retries_failed_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding_ids = ("R001", "R002")
            self._import_findings(root, finding_ids)
            counter = root / ".dls/cache/candidate-counter.txt"
            gate = root / ".dls/cache/bridge-gate"
            config = root / ".dls/config.toml"
            text = config.read_text(encoding="utf-8").replace(
                "print('75 tests passed')",
                (
                    "import pathlib; "
                    "pathlib.Path('.dls/cache/candidate-counter.txt').parent.mkdir(parents=True,exist_ok=True); "
                    "pathlib.Path('.dls/cache/candidate-counter.txt').open('a').write('test'+chr(10))"
                ),
            ).replace(
                "print('bridge passed')",
                (
                    "import pathlib,sys; "
                    "pathlib.Path('.dls/cache/candidate-counter.txt').open('a').write('bridge'+chr(10)); "
                    "ok=pathlib.Path('.dls/cache/bridge-gate').exists(); "
                    "print('waiting for bridge gate' if not ok else 'bridge passed'); "
                    "sys.exit(0 if ok else 9)"
                ),
            )
            config.write_text(text, encoding="utf-8")
            (root / "README.md").write_text("# Candidate\n", encoding="utf-8")
            git(root, "add", ".dls/config.toml", "README.md")
            git(root, "commit", "-m", "candidate")
            first = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            first_run = StateStore(root).load("C001")["candidate_runs"][-1]
            gate.parent.mkdir(parents=True, exist_ok=True)
            gate.write_text("ready", encoding="utf-8")
            second = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(second["status"], "completed")
            self.assertEqual(second["run_id"], first["run_id"])
            self.assertEqual(second["operation_id"], first_run["operation_id"])
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines(),
                ["test", "bridge", "bridge"],
            )

    def test_inherited_declaration_accepts_note_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding_ids = ("R001", "R002")
            self._import_findings(root, finding_ids)
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "print('75 tests passed')",
                    "import sys; sys.exit(7 if 'gate-pass' not in open('README.md').read() else 0)",
                ),
                encoding="utf-8",
            )
            (root / "README.md").write_text("# Candidate A\n", encoding="utf-8")
            git(root, "add", ".dls/config.toml", "README.md")
            git(root, "commit", "-m", "candidate A")
            candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            (root / "README.md").write_text("# Candidate B gate-pass\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate B")
            result = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=[],
                noted=["R001"],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["declaration_source"], "mixed")
            run = StateStore(root).load("C001")["candidate_runs"][-1]
            self.assertEqual(run["finding_dispositions"], {"R001": "note", "R002": "addressed"})

    def test_nearest_ancestor_declaration_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding_ids = ("R001", "R002")
            self._import_findings(root, finding_ids)
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "print('bridge passed')",
                    (
                        "import pathlib,sys; "
                        "sys.exit(0 if pathlib.Path('.dls/cache/gate').exists() else 8)"
                    ),
                ),
                encoding="utf-8",
            )
            (root / "README.md").write_text("# Candidate A\n", encoding="utf-8")
            git(root, "add", ".dls/config.toml", "README.md")
            git(root, "commit", "-m", "candidate A")
            candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            run_a = StateStore(root).load("C001")["candidate_runs"][-1]

            (root / "README.md").write_text("# Candidate B\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate B")
            candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            run_b = StateStore(root).load("C001")["candidate_runs"][-1]
            self.assertNotEqual(run_b["run_id"], run_a["run_id"])

            (root / "README.md").write_text("# Candidate C\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate C")
            (root / ".dls/cache/gate").write_text("ready", encoding="utf-8")
            completed = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(completed["status"], "completed")
            run_c = StateStore(root).load("C001")["candidate_runs"][-1]
            self.assertEqual(run_c["parent_run_id"], run_b["run_id"])

    def test_policy_drift_disables_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding_ids = ("R001", "R002")
            self._import_findings(root, finding_ids)
            (root / "README.md").write_text("# Candidate A\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate A")
            first = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(first["status"], "completed")

            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    "print('75 tests passed')",
                    "print('policy changed')",
                ),
                encoding="utf-8",
            )
            (root / "README.md").write_text("# Candidate B\n", encoding="utf-8")
            git(root, "add", ".dls/config.toml", "README.md")
            git(root, "commit", "-m", "candidate B")
            blocked = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(blocked["next_action"]["id"], "declare-finding-disposition")

            explicit = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
                dry_run=True,
            )
            self.assertEqual(explicit["status"], "projected")
            self.assertEqual(explicit["declaration_source"], "explicit")

    def test_legacy_run_and_divergent_head_do_not_inherit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding_ids = ("R001", "R002")
            _, first_pack = self._import_findings(root, finding_ids)
            (root / "README.md").write_text("# Candidate A\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate A")
            candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            state_path = StateStore(root).path("C001")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["candidate_runs"][-1].pop("candidate_run_contract", None)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            (root / "README.md").write_text("# Candidate B\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate B")
            legacy = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(legacy["next_action"]["id"], "declare-finding-disposition")

            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["candidate_runs"][-1]["candidate_run_contract"] = "dls-candidate-run/v2"
            state["candidate_runs"][-1]["active_finding_ids"] = ["R001"]
            state_path.write_text(json.dumps(state), encoding="utf-8")
            changed_set = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(
                changed_set["next_action"]["id"],
                "declare-finding-disposition",
            )

            # Restore the v2 finding contract, then move to a clean branch that is not a
            # descendant of the candidate run. Git ancestry must still block inheritance.
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["candidate_runs"][-1]["candidate_run_contract"] = "dls-candidate-run/v2"
            state["candidate_runs"][-1]["active_finding_ids"] = list(finding_ids)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            git(root, "checkout", "-b", "divergent", first_pack["head_sha"])
            (root / "README.md").write_text("# Divergent candidate\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "divergent candidate")
            divergent = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(divergent["next_action"]["id"], "declare-finding-disposition")

    def test_tampered_manifest_blocks_candidate_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding_ids = ("R001", "R002")
            self._import_findings(root, finding_ids)
            (root / "README.md").write_text("# Candidate A\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate A")
            candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            state = StateStore(root).load("C001")
            result_entry = next(
                item for item in reversed(state["reviews"]) if item.get("kind") == "result"
            )
            manifest_path = root / result_entry["remediation_manifest_path"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["definition_digest"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            (root / "README.md").write_text("# Candidate B\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate B")
            with self.assertRaisesRegex(IntegrityError, "manifest digest mismatch"):
                candidate_ready(
                    root,
                    change_id="C001",
                    base_ref=None,
                    addressed=[],
                    noted=[],
                    extra_commands=[],
                    operation_id=None,
                )

    def test_operation_id_cannot_cross_candidate_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding_ids = ("R001", "R002")
            self._import_findings(root, finding_ids)
            (root / "README.md").write_text("# Candidate A\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate A")
            candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id="fixed-operation",
            )
            (root / "README.md").write_text("# Candidate B\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate B")
            with self.assertRaisesRegex(IntegrityError, "another candidate contract"):
                candidate_ready(
                    root,
                    change_id="C001",
                    base_ref=None,
                    addressed=[],
                    noted=[],
                    extra_commands=[],
                    operation_id="fixed-operation",
                )

    def test_completed_candidate_seeds_descendant_before_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding_ids = ("R001", "R002")
            self._import_findings(root, finding_ids)
            (root / "README.md").write_text("# Candidate A\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate A")
            first = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(first["status"], "completed")
            first_run = StateStore(root).load("C001")["candidate_runs"][-1]
            (root / "README.md").write_text("# Candidate B\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate B")
            second = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertEqual(second["status"], "completed")
            second_run = StateStore(root).load("C001")["candidate_runs"][-1]
            self.assertEqual(second_run["parent_run_id"], first_run["run_id"])
            self.assertNotEqual(second["review_pack_path"], first["review_pack_path"])

    def test_concurrent_inherited_candidate_executes_each_command_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            finding_ids = ("R001", "R002")
            self._import_findings(root, finding_ids)
            counter = root / ".dls/cache/inherited-counter.txt"
            config = root / ".dls/config.toml"
            text = config.read_text(encoding="utf-8").replace(
                "print('75 tests passed')",
                (
                    "import pathlib,time; "
                    "pathlib.Path('.dls/cache/inherited-counter.txt').parent.mkdir(parents=True,exist_ok=True); "
                    "pathlib.Path('.dls/cache/inherited-counter.txt').open('a').write('test'+chr(10)); "
                    "time.sleep(1)"
                ),
            ).replace(
                "print('bridge passed')",
                (
                    "import pathlib; "
                    "pathlib.Path('.dls/cache/inherited-counter.txt').open('a').write('bridge'+chr(10))"
                ),
            )
            config.write_text(text, encoding="utf-8")
            (root / "README.md").write_text("# Candidate A\n", encoding="utf-8")
            git(root, "add", ".dls/config.toml", "README.md")
            git(root, "commit", "-m", "candidate A")
            candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=list(finding_ids),
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            (root / "README.md").write_text("# Candidate B\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "candidate B")
            barrier = threading.Barrier(2)

            def run() -> dict:
                barrier.wait()
                return candidate_ready(
                    root,
                    change_id="C001",
                    base_ref=None,
                    addressed=[],
                    noted=[],
                    extra_commands=[],
                    operation_id=None,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: run(), range(2)))
            self.assertIn("completed", {item["status"] for item in results})
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines(),
                ["test", "bridge", "test", "bridge"],
            )

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
