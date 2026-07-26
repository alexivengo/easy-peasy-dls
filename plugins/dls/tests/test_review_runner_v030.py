from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from dls_core.errors import IntegrityError
from dls_core.io import utc_now
from dls_core.operations import (
    REVIEW_RUNNER_CONTRACT,
    _codex_usage_from_output,
    _review_pack_digest,
    _validate_review_pack,
    _validate_review_report,
    approve,
    evidence_add,
    finding_disposition,
    remediation_start,
    review_import,
    review_pack,
    review_ready,
    ticket_set,
)
from dls_core.review_runner import (
    _codex_failure_reason,
    _derive_review_verdict,
    _derive_ticket_verdicts,
    _validate_strict_output_schema,
    _validate_structured_payload,
    review_run,
    review_status,
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


class ReviewRunnerV030Tests(unittest.TestCase):
    def test_model_output_schema_is_strict_and_failure_reason_is_actionable(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "assets"
            / "schemas"
            / "review-decision.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        _validate_strict_output_schema(schema)
        broken = json.loads(json.dumps(schema))
        broken["properties"]["optional_field"] = {"type": "string"}
        with self.assertRaisesRegex(IntegrityError, "missing=optional_field"):
            _validate_strict_output_schema(broken)
        transcript = (
            b'{"type":"turn.failed","error":{"message":"'
            b'{\\"error\\":{\\"code\\":\\"invalid_json_schema\\",'
            b'\\"message\\":\\"missing required field\\"}}"}}\n'
        )
        self.assertEqual(
            _codex_failure_reason(transcript, exit_code=1),
            "invalid_json_schema: missing required field",
        )

    def test_runner_derives_stage_correct_ticket_verdicts(self) -> None:
        pack = {"tickets": {"T01": {}, "T04": {}}}
        findings = [
            {
                "id": "R-CODE",
                "severity": "should-fix",
                "kind": "defect",
                "ticket_ids": ["T01"],
                "blocks": ["review", "acceptance"],
            },
            {
                "id": "R-RELEASE",
                "severity": "note",
                "kind": "external",
                "ticket_ids": ["T04"],
                "blocks": ["release", "production"],
            },
        ]
        verdicts = _derive_ticket_verdicts(pack, findings)
        self.assertEqual(
            verdicts,
            [
                {
                    "ticket_id": "T01",
                    "verdict": "not-clear",
                    "finding_ids": ["R-CODE"],
                },
                {
                    "ticket_id": "T04",
                    "verdict": "clear",
                    "finding_ids": ["R-RELEASE"],
                },
            ],
        )
        self.assertEqual(_derive_review_verdict(verdicts), "not-clear")

    def test_semantic_ticket_verdicts_are_optional_and_usage_is_local(self) -> None:
        _validate_structured_payload(
            {
                "verdict": "review-clear",
                "summary": "clear",
                "findings": [],
                "prior_finding_verdicts": [],
            },
            payload_kind="decision",
            lens_id=None,
        )
        usage = _codex_usage_from_output(
            b"diagnostic\n"
            b'{"type":"turn.completed","usage":{"input_tokens":12,'
            b'"cached_input_tokens":9,"output_tokens":3,'
            b'"reasoning_output_tokens":2}}\n'
        )
        self.assertEqual(
            usage,
            {
                "input_tokens": 12,
                "cached_input_tokens": 9,
                "output_tokens": 3,
                "reasoning_output_tokens": 2,
            },
        )

    def _prepared_standard(self, root: Path) -> tuple[str, dict]:
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
            operation_id="definition",
        )
        git(root, "add", ".dls", "docs")
        git(root, "commit", "-m", "review candidate")
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
            operation_id="evidence",
        )
        pack = review_pack(
            root,
            change_id="C001",
            base_ref=base_sha,
            head_ref=None,
            expected_revision=3,
            advisory_dirty=False,
            operation_id="pack",
        )
        self.assertEqual(
            pack["review_pack"]["runner_contract"],
            REVIEW_RUNNER_CONTRACT,
        )
        return base_sha, pack

    def _install_fake_codex(
        self,
        root: Path,
        *,
        native_sleep: float = 0.0,
        invalid_semantic_once: bool = False,
        semantic_exit: int = 0,
        native_exit: int = 0,
        reconciliation_blocker: bool = False,
    ) -> tuple[str | None, Path, Path]:
        fake_bin = root / ".dls" / "cache" / "runner-fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        counter = root / ".dls" / "cache" / "runner-calls.log"
        marker = root / ".dls" / "cache" / "native-entered"
        invalid_marker = root / ".dls" / "cache" / "invalid-semantic-used"
        state_path = root / ".dls" / "state" / "C001.json"
        executable = fake_bin / "codex"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, re, sys, time\n"
            "from pathlib import Path\n"
            f"counter = Path({str(counter)!r})\n"
            f"marker = Path({str(marker)!r})\n"
            f"invalid_marker = Path({str(invalid_marker)!r})\n"
            f"state_path = Path({str(state_path)!r})\n"
            f"owner_root = Path({str(root)!r}).resolve()\n"
            "args = sys.argv[1:]\n"
            "output = Path(args[args.index('--output-last-message') + 1])\n"
            "native = 'review' in args\n"
            "kind = 'native'\n"
            "if native:\n"
            "    if Path.cwd().resolve() == owner_root:\n"
            "        raise SystemExit(65)\n"
            "    state = json.loads(state_path.read_text())\n"
            "    if not any(item.get('lane_key') == 'native' and "
            "item.get('status') == 'running' for item in state['reviews']):\n"
            "        raise SystemExit(66)\n"
            "    marker.write_text('running')\n"
            f"    time.sleep({native_sleep!r})\n"
            f"    if {native_exit!r}:\n"
            "        counter.parent.mkdir(parents=True, exist_ok=True)\n"
            "        with counter.open('a') as handle: handle.write(kind + '\\n')\n"
            f"        raise SystemExit({native_exit!r})\n"
            "    output.parent.mkdir(parents=True, exist_ok=True)\n"
            "    output.write_text(json.dumps({'summary': 'No findings.', "
            "'findings': []}))\n"
            "else:\n"
            "    prompt = Path('.dls-review-input/prompt.md').read_text()\n"
            "    context = json.loads(Path('.dls-review-input/context.json').read_text())\n"
            "    pack_item = next(item for item in context['inputs'] "
            "if item.get('reason') == 'active-review-pack')\n"
            "    pack = json.loads(Path(pack_item['path']).read_text())\n"
            "    prior_verdicts = [{'finding_id': item['finding_id'], "
            "'verdict': 'verified', 'replacement_finding_id': None, "
            "'evidence': ['verified by fake reviewer']} for item in "
            "pack.get('required_prior_findings', [])]\n"
            "    if prompt.startswith('# DLS specialist review'):\n"
            "        kind = 'specialist'\n"
            "        if Path('.dls-review-input/native.txt').exists():\n"
            "            raise SystemExit(67)\n"
            "        lens = re.search(r'`([^`]+)`:', prompt).group(1)\n"
            "        payload = {'lens_id': lens, 'summary': 'clear', "
            "'findings': []}\n"
            "    else:\n"
            "        if prompt.startswith('# DLS independent semantic review'):\n"
            "            kind = 'semantic-independent'\n"
            "            if Path('.dls-review-input/native.txt').exists():\n"
            "                raise SystemExit(68)\n"
            f"            if {semantic_exit!r}:\n"
            "                counter.parent.mkdir(parents=True, exist_ok=True)\n"
            "                with counter.open('a') as handle: "
            "handle.write(kind + '\\n')\n"
            "                print(json.dumps({'type': 'turn.failed', "
            "'error': {'message': json.dumps({'error': {"
            "'code': 'synthetic_model_failure', "
            "'message': 'semantic lane failed'}})}}))\n"
            f"                raise SystemExit({semantic_exit!r})\n"
            f"            if {invalid_semantic_once!r} and not invalid_marker.exists():\n"
            "                invalid_marker.write_text('used')\n"
            "                output.write_text('{}')\n"
            "                counter.parent.mkdir(parents=True, exist_ok=True)\n"
            "                with counter.open('a') as handle: "
            "handle.write(kind + '\\n')\n"
            "                raise SystemExit(0)\n"
            "        elif prompt.startswith('# DLS review reconciliation'):\n"
            "            kind = 'reconciliation'\n"
            "            if not Path('.dls-review-input/native.txt').exists():\n"
            "                raise SystemExit(69)\n"
            "        elif prompt.startswith('# DLS remediation final-full review'):\n"
            "            kind = 'final-full'\n"
            "        payload = {'verdict': 'review-clear', 'summary': 'clear', "
            "'findings': [], 'prior_finding_verdicts': prior_verdicts}\n"
            f"        if {reconciliation_blocker!r} and kind == 'reconciliation':\n"
            "            payload['verdict'] = 'not-clear'\n"
            "            payload['summary'] = 'blocker remains'\n"
            "            payload['findings'] = [{'id': 'RNEW', "
            "'severity': 'blocker', 'kind': 'defect', "
            "'location': 'README.md', 'issue': 'A defect remains.', "
            "'impact': 'Review cannot clear.', "
            "'required_fix': 'Fix the defect.', 'ticket_ids': [], "
            "'requirement_ids': [], 'blocks': ['review', 'acceptance'], "
            "'provenance': ['reconciliation']}]\n"
            "    output.parent.mkdir(parents=True, exist_ok=True)\n"
            "    output.write_text(json.dumps(payload))\n"
            "counter.parent.mkdir(parents=True, exist_ok=True)\n"
            "with counter.open('a') as handle: handle.write(kind + '\\n')\n"
            "print(json.dumps({'type': 'fake', 'lane': kind}))\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        original_path = os.environ.get("PATH")
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{original_path or ''}"
        return original_path, counter, marker

    def _restore_path(self, original_path: str | None) -> None:
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path

    def test_review_run_is_single_flight_and_imports_state_owned_reviewir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, pack = self._prepared_standard(root)
            original, counter, marker = self._install_fake_codex(
                root,
                native_sleep=0.5,
            )
            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    first = executor.submit(
                        review_run,
                        root,
                        change_id="C001",
                        pack_path=None,
                        operation_id="review-root",
                    )
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(marker.exists())
                    running = review_status(root, change_id="C001")
                    self.assertEqual(running["status"], "running")
                    duplicate = review_run(
                        root,
                        change_id="C001",
                        pack_path=None,
                        operation_id="another-root",
                    )
                    self.assertEqual(duplicate["status"], "running")
                    self.assertEqual(
                        duplicate["next_action"]["id"],
                        "wait-review",
                    )
                    completed = first.result(timeout=20)
            finally:
                self._restore_path(original)

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["verdict"], "review-clear")
            self.assertTrue(completed["review_result_path"])
            self.assertEqual(
                completed["presentation"]["contract"],
                "codex-inline-comments/v1",
            )
            self.assertTrue(completed["presentation"]["exact_head"])
            self.assertEqual(completed["presentation"]["comments"], [])
            calls = counter.read_text(encoding="utf-8").splitlines()
            self.assertEqual(calls.count("native"), 1)
            self.assertEqual(
                calls,
                ["native", "semantic-independent", "reconciliation"],
            )
            result = json.loads(
                (root / completed["review_result_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(result["runner_contract"], REVIEW_RUNNER_CONTRACT)
            self.assertEqual(result["review_id"], pack["review_id"])
            self.assertIn("reconciliation", result["lanes"])
            status = review_status(root, change_id="C001")
            self.assertEqual(status["status"], "completed")
            self.assertEqual(
                status["review_result_path"],
                completed["review_result_path"],
            )
            self.assertEqual(status["presentation"], completed["presentation"])
            repeated = review_run(
                root,
                change_id="C001",
                pack_path=None,
                operation_id="review-root",
            )
            self.assertEqual(repeated["status"], "completed")
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines(),
                calls,
            )

            attempts = [
                item
                for item in StateStore(root).load("C001")["reviews"]
                if item.get("lane_key")
            ]
            self.assertTrue(all(item["status"] == "completed" for item in attempts))
            for attempt in attempts:
                self.assertIn("--ignore-user-config", attempt["argv"])
                self.assertIn("--ephemeral", attempt["argv"])
                self.assertTrue(attempt.get("context_digest"))
                self.assertTrue(attempt.get("prompt_digest"))
                self.assertTrue(attempt.get("schema_digest"))

    def test_review_run_returns_unprepared_candidate_to_implementation_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepared_standard(root)
            original, counter, _ = self._install_fake_codex(
                root,
                reconciliation_blocker=True,
            )
            try:
                completed = review_run(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="first-review",
                )
            finally:
                self._restore_path(original)
            self.assertEqual(completed["verdict"], "not-clear")
            self.assertTrue(completed["review_result_path"])
            self.assertTrue(completed["remediation_manifest_path"])
            prior_review_id = completed["review_id"]
            prior_result_path = completed["review_result_path"]
            calls_before = counter.read_text(encoding="utf-8")

            (root / "README.md").write_text(
                "# Fixture\n\nRemediation candidate without handoff.\n",
                encoding="utf-8",
            )
            git(root, "add", "README.md")
            git(root, "commit", "-m", "advance without candidate-ready")

            status = review_status(root, change_id="C001")
            self.assertEqual(status["status"], "not-prepared")
            self.assertEqual(status["next_action"]["id"], "prepare-candidate")
            self.assertIsNone(status["review_id"])
            self.assertIsNone(status["review_result_path"])
            self.assertIsNone(status["verdict"])
            self.assertEqual(status["prior_review_id"], prior_review_id)
            self.assertEqual(
                status["prior_review_result_path"],
                prior_result_path,
            )

            blocked = review_run(
                root,
                change_id="C001",
                pack_path=None,
                operation_id="must-not-start-models",
            )
            self.assertTrue(blocked["ok"])
            self.assertEqual(blocked["status"], "not-prepared")
            self.assertEqual(blocked["next_action"]["id"], "prepare-candidate")
            self.assertIsNone(blocked["review_result_path"])
            self.assertIsNone(blocked["review_pack_path"])
            self.assertEqual(counter.read_text(encoding="utf-8"), calls_before)

    def test_invalid_semantic_output_retries_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepared_standard(root)
            original, counter, _ = self._install_fake_codex(
                root,
                invalid_semantic_once=True,
            )
            try:
                completed = review_run(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="retry-root",
                )
            finally:
                self._restore_path(original)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines().count(
                    "semantic-independent"
                ),
                2,
            )
            semantic = [
                item
                for item in StateStore(root).load("C001")["reviews"]
                if item.get("lane_key") == "semantic:full"
            ]
            self.assertEqual(
                [item["status"] for item in semantic],
                ["invalid-output", "completed"],
            )

    def test_changed_lane_contract_retries_without_reusing_native_or_operation_id(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, pack_result = self._prepared_standard(root)
            pack = pack_result["review_pack"]
            start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="legacy-native",
            )
            store = StateStore(root)
            _, _, claimed = store.claim_review_lane(
                "C001",
                attempt={
                    "review_id": pack["review_id"],
                    "kind": "semantic",
                    "lane_key": "semantic:full",
                    "attempt_id": "legacy-semantic-failure",
                    "attempt_ordinal": 1,
                    "operation_id": "shared-root:semantic-full",
                    "runner_pid": os.getpid(),
                    "runner_contract": REVIEW_RUNNER_CONTRACT,
                    "head_sha": pack["head_sha"],
                    "pack_digest": pack["pack_digest"],
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "high",
                    "schema_digest": "legacy-invalid-schema",
                    "started_at": utc_now(),
                },
                operation_kind="review-run:semantic:full",
            )
            self.assertTrue(claimed)
            store.finish_review_lane(
                "C001",
                attempt_id="legacy-semantic-failure",
                expected_status="running",
                updates={
                    "status": "failed",
                    "failure_reason": "invalid_json_schema",
                    "completed_at": utc_now(),
                },
            )
            projected = review_status(root, change_id="C001")
            self.assertEqual(projected["next_action"]["id"], "retry-review")
            original, counter, _ = self._install_fake_codex(root)
            try:
                completed = review_run(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="shared-root",
                )
            finally:
                self._restore_path(original)
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines(),
                ["semantic-independent", "reconciliation"],
            )
            state = StateStore(root).load("C001")
            semantic = [
                item
                for item in state["reviews"]
                if item.get("lane_key") == "semantic:full"
            ]
            self.assertEqual(
                [item["status"] for item in semantic],
                ["failed", "completed"],
            )
            self.assertNotEqual(
                semantic[0]["operation_id"],
                semantic[1]["operation_id"],
            )
            self.assertIn(pack["review_id"], semantic[1]["operation_id"])

    def test_review_run_dry_run_projects_pipeline_without_model_or_state_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepared_standard(root)
            before = StateStore(root).load("C001")["state_revision"]
            original, counter, _ = self._install_fake_codex(root)
            try:
                projected = review_run(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="dry-root",
                    dry_run=True,
                )
            finally:
                self._restore_path(original)
            self.assertEqual(projected["status"], "ready")
            self.assertEqual(
                projected["projected_lanes"]["semantic"],
                "full",
            )
            self.assertFalse(counter.exists())
            self.assertEqual(
                StateStore(root).load("C001")["state_revision"],
                before,
            )

    def test_critical_review_runs_three_deterministic_specialists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha = initialize_git(root)
            initialize(root)
            create_change(
                root,
                control="critical",
                impacts=[
                    "architecture",
                    "concurrency",
                    "data-migration",
                    "public-api",
                ],
                tickets=True,
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
                operation_id="definition",
            )
            ticket_set(
                root,
                change_id="C001",
                ticket_id="T01",
                ticket_status="in-progress",
                expected_revision=2,
                note=None,
                operation_id="ticket-start",
            )
            ticket_set(
                root,
                change_id="C001",
                ticket_id="T01",
                ticket_status="implemented",
                expected_revision=3,
                note=None,
                operation_id="ticket-implemented",
            )
            git(root, "add", ".dls", "docs")
            git(root, "commit", "-m", "critical candidate")
            head_sha = git(root, "rev-parse", "HEAD")
            evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=0,
                summary="PASS",
                expected_revision=4,
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="evidence",
            )
            pack = review_pack(
                root,
                change_id="C001",
                base_ref=base_sha,
                head_ref=None,
                expected_revision=5,
                advisory_dirty=False,
                operation_id="critical-pack",
            )
            expected_lenses = [
                "contract-trust",
                "concurrency-reliability",
                "data-migration",
            ]
            self.assertEqual(
                [item["id"] for item in pack["review_pack"]["risk_lenses"]],
                expected_lenses,
            )
            original, counter, _ = self._install_fake_codex(root)
            try:
                completed = review_run(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="critical-root",
                )
            finally:
                self._restore_path(original)
            self.assertEqual(completed["verdict"], "review-clear")
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines(),
                [
                    "native",
                    "specialist",
                    "specialist",
                    "specialist",
                    "semantic-independent",
                    "reconciliation",
                ],
            )
            result = json.loads(
                (root / completed["review_result_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["lens_id"] for item in result["lanes"]["specialists"]],
                expected_lenses,
            )
            self.assertEqual(
                result["lanes"]["semantic"]["reasoning_effort"],
                "xhigh",
            )

    def test_remediation_runs_targeted_reconciliation_then_final_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha, first_pack = self._prepared_standard(root)
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="first-native",
            )
            finding = {
                "id": "R001",
                "severity": "should-fix",
                "kind": "defect",
                "location": "README.md",
                "issue": "The candidate needs remediation.",
                "impact": "Acceptance is not yet clear.",
                "required_fix": "Update the candidate.",
                "ticket_ids": [],
                "requirement_ids": [],
                "base_sha": first_pack["review_pack"]["base_sha"],
                "head_sha": first_pack["review_pack"]["head_sha"],
                "blocks": ["review", "acceptance"],
            }
            report = build_review_report(
                root,
                pack_result=first_pack,
                start_result=started,
                verdict="not-clear",
                findings=[finding],
            )
            report_path = root / ".dls/cache/first-review.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/first-review.json",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                operation_id="first-import",
            )
            remediation_start(root, change_id="C001")
            (root / "README.md").write_text(
                "# Fixture\n\nRemediated.\n",
                encoding="utf-8",
            )
            git(root, "add", "README.md")
            git(root, "commit", "-m", "remediate review")
            head_sha = git(root, "rev-parse", "HEAD")
            evidence = evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=0,
                summary="PASS remediation",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="remediation-evidence",
            )
            finding_disposition(
                root,
                change_id="C001",
                finding_id="R001",
                disposition_status="addressed",
                rationale="The candidate was updated and validated.",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                git_sha=head_sha,
                evidence=[evidence["evidence_path"]],
                actor="codex",
                prompt=None,
                response=None,
                operation_id="address-finding",
            )
            ready = review_ready(
                root,
                change_id="C001",
                base_ref=base_sha,
                expected_revision=StateStore(root).load("C001")["state_revision"],
                operation_id="remediation-ready",
            )
            self.assertEqual(ready["review_pack"]["review_mode"], "remediation")

            original, counter, _ = self._install_fake_codex(root)
            try:
                completed = review_run(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="remediation-root",
                )
            finally:
                self._restore_path(original)
            self.assertEqual(completed["verdict"], "review-clear")
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines(),
                [
                    "native",
                    "semantic-independent",
                    "reconciliation",
                    "final-full",
                ],
            )
            result = json.loads(
                (root / completed["review_result_path"]).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["kind"] for item in result["lanes"]["semantic"]["passes"]],
                ["targeted", "final-full"],
            )
            self.assertEqual(
                result["prior_finding_verdicts"][0]["finding_id"],
                "R001",
            )

    def test_review_status_reports_failed_without_restarting_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepared_standard(root)
            original, counter, _ = self._install_fake_codex(root, native_exit=9)
            try:
                with self.assertRaisesRegex(IntegrityError, "status=failed"):
                    review_run(
                        root,
                        change_id="C001",
                        pack_path=None,
                        operation_id="failed-root",
                    )
                failed = review_status(root, change_id="C001")
            finally:
                self._restore_path(original)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines(),
                ["native"],
            )

    def test_semantic_api_failure_finalizes_pipeline_and_exposes_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepared_standard(root)
            original, counter, _ = self._install_fake_codex(
                root,
                semantic_exit=7,
            )
            try:
                with self.assertRaisesRegex(
                    IntegrityError,
                    "synthetic_model_failure: semantic lane failed",
                ):
                    review_run(
                        root,
                        change_id="C001",
                        pack_path=None,
                        operation_id="semantic-failure-root",
                    )
                failed = review_status(root, change_id="C001", verbose=True)
            finally:
                self._restore_path(original)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(
                failed["next_action"]["id"],
                "inspect-review-failure",
            )
            self.assertEqual(failed["pipeline"]["status"], "failed")
            self.assertEqual(failed["pipeline"]["stage"], "semantic:full")
            self.assertIn(
                "synthetic_model_failure",
                failed["pipeline"]["failure_reason"],
            )
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines(),
                ["native", "semantic-independent"],
            )

    def test_finalize_failure_is_visible_and_resume_reuses_completed_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._prepared_standard(root)
            original, counter, _ = self._install_fake_codex(root)
            try:
                with mock.patch(
                    "dls_core.review_runner._build_review_ir",
                    side_effect=IntegrityError("synthetic assembly failure"),
                ):
                    with self.assertRaisesRegex(
                        IntegrityError,
                        "synthetic assembly failure",
                    ):
                        review_run(
                            root,
                            change_id="C001",
                            pack_path=None,
                            operation_id="broken-finalize",
                        )
                calls_before_resume = counter.read_text(encoding="utf-8").splitlines()
                failed = review_status(root, change_id="C001")
                self.assertEqual(failed["status"], "failed-finalize")
                self.assertEqual(failed["next_action"]["id"], "resume-review")
                self.assertEqual(failed["progress"]["stage"], "finalizing")
                self.assertNotIn("argv", json.dumps(failed["lanes"]))

                completed = review_run(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="resume-finalize",
                )
                self.assertEqual(completed["status"], "completed")
                self.assertTrue(completed["review_result_path"])
                self.assertEqual(
                    counter.read_text(encoding="utf-8").splitlines(),
                    calls_before_resume,
                )
                verbose = review_status(root, change_id="C001", verbose=True)
                self.assertIn("lane_details", verbose)
                self.assertEqual(verbose["pipeline"]["status"], "completed")
            finally:
                self._restore_path(original)

    def test_orphaned_native_attempt_is_abandoned_then_retried_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, pack_result = self._prepared_standard(root)
            pack = pack_result["review_pack"]
            store = StateStore(root)
            store.claim_review_lane(
                "C001",
                attempt={
                    "review_id": pack["review_id"],
                    "kind": "native",
                    "lane_key": "native",
                    "attempt_id": "orphan-attempt",
                    "attempt_ordinal": 1,
                    "operation_id": "orphan-operation",
                    "runner_pid": 99999999,
                    "runner_contract": REVIEW_RUNNER_CONTRACT,
                    "base_sha": pack["comparison_base_sha"],
                    "head_sha": pack["head_sha"],
                    "pack_digest": pack["pack_digest"],
                    "started_at": utc_now(),
                },
                operation_kind="review-start",
            )
            original, counter, _ = self._install_fake_codex(root)
            try:
                completed = review_run(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="orphan-root",
                )
            finally:
                self._restore_path(original)
            self.assertEqual(completed["status"], "completed")
            native_attempts = [
                item
                for item in StateStore(root).load("C001")["reviews"]
                if item.get("lane_key") == "native"
            ]
            self.assertEqual(
                [item["status"] for item in native_attempts],
                ["abandoned", "completed"],
            )
            self.assertEqual(
                counter.read_text(encoding="utf-8").splitlines().count("native"),
                1,
            )

    def test_remediation_blocker_imports_not_clear_without_final_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base_sha, first_pack = self._prepared_standard(root)
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="first-native",
            )
            old_finding = {
                "id": "R001",
                "severity": "should-fix",
                "kind": "defect",
                "location": "README.md",
                "issue": "The candidate needs remediation.",
                "impact": "Acceptance is not yet clear.",
                "required_fix": "Update the candidate.",
                "ticket_ids": [],
                "requirement_ids": [],
                "base_sha": first_pack["review_pack"]["base_sha"],
                "head_sha": first_pack["review_pack"]["head_sha"],
                "blocks": ["review", "acceptance"],
            }
            report = build_review_report(
                root,
                pack_result=first_pack,
                start_result=started,
                verdict="not-clear",
                findings=[old_finding],
            )
            path = root / ".dls/cache/first-review.json"
            path.write_text(json.dumps(report), encoding="utf-8")
            review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/first-review.json",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                operation_id="first-import",
            )
            remediation_start(root, change_id="C001")
            (root / "README.md").write_text("# Fixture\n\nAttempted fix.\n")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "attempt remediation")
            head_sha = git(root, "rev-parse", "HEAD")
            evidence = evidence_add(
                root,
                change_id="C001",
                command_id="test",
                exit_code=0,
                summary="PASS remediation",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                git_sha=head_sha,
                artifacts=[],
                environment="fixture",
                duration_seconds=0.1,
                operation_id="remediation-evidence",
            )
            finding_disposition(
                root,
                change_id="C001",
                finding_id="R001",
                disposition_status="addressed",
                rationale="The candidate was updated and validated.",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                git_sha=head_sha,
                evidence=[evidence["evidence_path"]],
                actor="codex",
                prompt=None,
                response=None,
                operation_id="address-finding",
            )
            review_ready(
                root,
                change_id="C001",
                base_ref=base_sha,
                expected_revision=StateStore(root).load("C001")["state_revision"],
                operation_id="remediation-ready",
            )
            original, counter, _ = self._install_fake_codex(
                root,
                reconciliation_blocker=True,
            )
            try:
                completed = review_run(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="blocker-root",
                )
            finally:
                self._restore_path(original)
            self.assertTrue(completed["ok"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["verdict"], "not-clear")
            self.assertTrue(completed["review_result_path"])
            self.assertTrue(completed["presentation"]["renderable"])
            self.assertEqual(
                [
                    item["finding_id"]
                    for item in completed["presentation"]["comments"]
                ],
                ["RNEW"],
            )
            self.assertIn(
                "::code-comment{",
                completed["presentation"]["comments"][0]["directive"],
            )
            self.assertNotIn(
                "final-full",
                counter.read_text(encoding="utf-8").splitlines(),
            )

    def test_import_rejects_self_declared_runner_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, pack = self._prepared_standard(root)
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="native",
            )
            report = build_review_report(
                root,
                pack_result=pack,
                start_result=started,
                verdict="review-clear",
            )
            report["lanes"]["semantic"]["attempt_id"] = "self-declared"
            report_path = root / ".dls/cache/self-declared.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(
                IntegrityError,
                "no completed state attempt",
            ):
                review_import(
                    root,
                    change_id="C001",
                    report_path=".dls/cache/self-declared.json",
                    expected_revision=StateStore(root).load("C001")[
                        "state_revision"
                    ],
                    operation_id="self-declared-import",
                )

    def test_legacy_reviewpack_and_reviewir_v2_without_marker_remain_readable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, pack_result = self._prepared_standard(root)
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="native",
            )
            legacy_pack_result = json.loads(json.dumps(pack_result))
            legacy_pack = legacy_pack_result["review_pack"]
            legacy_pack.pop("runner_contract")
            legacy_pack["pack_digest"] = _review_pack_digest(legacy_pack)
            _validate_review_pack(legacy_pack, "C001")
            report = build_review_report(
                root,
                pack_result=legacy_pack_result,
                start_result=started,
                verdict="review-clear",
            )
            self.assertNotIn("runner_contract", report)
            _validate_review_report(report, "C001", legacy_pack)

    def test_lane_finish_preserves_parallel_state_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            create_change(root, control="standard")
            store = StateStore(root)
            attempt = {
                "review_id": "review",
                "lane_key": "semantic:full",
                "attempt_id": "attempt",
                "operation_id": "lane-operation",
                "kind": "semantic",
                "runner_pid": os.getpid(),
                "runner_contract": REVIEW_RUNNER_CONTRACT,
                "started_at": utc_now(),
            }
            claimed, _, did_claim = store.claim_review_lane(
                "C001",
                attempt=attempt,
                operation_kind="review-run:semantic:full",
            )
            self.assertTrue(did_claim)
            store.mutate(
                "C001",
                expected_revision=claimed["state_revision"],
                operation_id="parallel-update",
                operation_kind="parallel-fixture",
                mutator=lambda state: state.update({"parallel_marker": "kept"}),
            )
            finished, _, changed = store.finish_review_lane(
                "C001",
                attempt_id="attempt",
                expected_status="running",
                updates={"status": "completed", "completed_at": utc_now()},
            )
            self.assertTrue(changed)
            self.assertEqual(finished["parallel_marker"], "kept")

    def test_skill_forwards_to_review_run_without_path_probe_or_subagents(self) -> None:
        plugin_root = Path(__file__).resolve().parents[1]
        skill = (
            plugin_root / "skills" / "dls-workflow" / "SKILL.md"
        ).read_text(encoding="utf-8")
        review = (
            plugin_root
            / "skills"
            / "dls-workflow"
            / "references"
            / "review.md"
        ).read_text(encoding="utf-8")
        remediation = (
            plugin_root
            / "skills"
            / "dls-workflow"
            / "references"
            / "remediation.md"
        ).read_text(encoding="utf-8")
        combined = skill + "\n" + review
        self.assertIn("review-run", combined)
        self.assertNotIn("command -v dls", combined)
        self.assertNotIn("which dls", combined)
        self.assertIn("Do not create subagents", review)
        self.assertNotIn("review-import CHANGE_ID", review)
        self.assertIn("presentation.comments[].directive", review)
        self.assertIn("::code-comment", combined)
        self.assertIn("one compact unchanged heartbeat every 60–90 seconds", review)
        self.assertIn("regardless of whether the primary command has emitted", review)
        self.assertIn("failed-finalize", review)
        self.assertIn("same stable operation ID", review)
        self.assertIn("prepare-candidate", review)
        self.assertIn("return to the implementation", review)
        self.assertIn("do not run validation", review)
        self.assertIn("open-review-task", remediation)
        self.assertIn("candidate-ready", remediation)
        self.assertIn("candidate-status", remediation)
        self.assertNotIn("--expect-revision", remediation)
        self.assertNotIn("dls finding set", remediation)
        self.assertNotIn("dls evidence add", remediation)
        self.assertNotIn("review-ready CHANGE_ID", remediation)
        self.assertIn("Never invoke `review-run`", remediation)
        self.assertNotIn(" dls review-run ", remediation)


if __name__ == "__main__":
    unittest.main()
