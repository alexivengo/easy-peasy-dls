from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from dls_core.cli import build_parser
from dls_core.core import (
    approve,
    decision_projection,
    dependency_set,
    definition_digest,
    load_state,
    mutate_state,
    stable_digest,
    status,
    ticket_set,
    upgrade,
)
from dls_core.errors import IntegrityError
from dls_core.runner import _model_call, _run_bounded, candidate_ready, review_run
from dls_core.worktrees import prepare, resolve_change_root
from support import (
    FAKE_CODEX,
    FAKE_REPAIR,
    FAKE_CONFLICT,
    FAKE_BUDGET,
    change,
    commit,
    configure,
    fake_codex,
    git,
    repository,
    restore_environment,
)


class CoreResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.base = repository(self.root)
        configure(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fake(self, mode: str = "clear") -> tuple[Path, dict[str, str | None]]:
        executable, previous = fake_codex(self.root, FAKE_CODEX)
        executable.with_name("mode").write_text(mode, encoding="utf-8")
        return executable, previous

    @staticmethod
    def _calls(path: str) -> list[str]:
        candidate = Path(path)
        return candidate.read_text().splitlines() if candidate.is_file() else []

    def _approve(self, change_id: str, *, architecture: bool = False) -> None:
        state = load_state(self.root, change_id)
        projection = decision_projection(self.root, state)
        response = f"definition {projection['definition']['digest'][:12]}"
        if architecture:
            response += f" architecture {projection['architecture']['digest'][:12]}"
        approve(
            self.root,
            change_id=change_id,
            decision="definition",
            include_design=False,
            include_architecture=architecture,
            actor="user",
            response=response,
            git_sha=git(self.root, "rev-parse", "HEAD"),
            dry_run=False,
        )

    def _prepare_code(
        self,
        *,
        control: str,
        impacts: list[str] | None = None,
        architecture: bool = False,
    ) -> tuple[str, dict[str, str | None]]:
        change(self.root, control=control, impacts=impacts, adr=architecture)
        commit(self.root, "definition")
        executable, previous = self._fake()
        if control in {"standard", "critical"}:
            result = review_run(self.root, change_id="C001", kind="definition")
            self.assertEqual("review-clear", result["verdict"])
        self._approve("C001", architecture=architecture)
        ticket_set(
            self.root,
            change_id="C001",
            ticket_id="C001-T01",
            value="implemented",
            note=None,
        )
        (self.root / "src.py").write_text("value = 1\n", encoding="utf-8")
        commit(self.root, "implementation")
        ready = candidate_ready(
            self.root,
            change_id="C001",
            base=self.base,
            addressed=[],
            noted=[],
            dry_run=False,
        )
        self.assertEqual("open-review-task", ready["next_action"]["id"])
        return str(executable.with_name("calls.jsonl")), previous

    def test_separate_atomic_approvals_and_staleness(self) -> None:
        change(self.root, control="critical", impacts=["architecture"], adr=True)
        commit(self.root, "definition")
        executable, previous = self._fake()
        try:
            review_run(self.root, change_id="C001", kind="definition")
            self._approve("C001", architecture=True)
            state = load_state(self.root, "C001")
            self.assertEqual(
                {"definition", "architecture"},
                {item["decision"] for item in state["approvals"] if item["status"] == "current"},
            )
            spec = self.root / state["change"]["artifacts"]["spec"]["path"]
            spec.write_text(spec.read_text() + "\nChanged.\n", encoding="utf-8")
            self.assertEqual("run-definition-review", status(self.root, "C001")["next_action"]["id"])
        finally:
            restore_environment(previous)

    def test_approval_bundle_retry_is_idempotent(self) -> None:
        change(self.root, control="routine")
        commit(self.root, "definition")
        projection = decision_projection(self.root, load_state(self.root, "C001"))
        arguments = {
            "change_id": "C001",
            "decision": "definition",
            "include_design": False,
            "include_architecture": False,
            "actor": "user",
            "response": f"definition {projection['definition']['digest'][:12]}",
            "git_sha": git(self.root, "rev-parse", "HEAD"),
            "dry_run": False,
        }
        first = approve(self.root, **arguments)
        second = approve(self.root, **arguments)
        self.assertEqual(first["state_revision"], second["state_revision"])
        self.assertEqual(first["approvals"][0]["id"], second["approvals"][0]["id"])
        self.assertEqual(1, len(load_state(self.root, "C001")["approvals"]))

    def test_design_decision_is_derived_from_committed_spec(self) -> None:
        change(self.root, control="routine", impacts=["user-interface"])
        state = load_state(self.root, "C001")
        spec = self.root / state["change"]["artifacts"]["change"]["path"]
        text = spec.read_text(encoding="utf-8")
        text = text.replace(
            "<!-- dls:design:start -->\n"
            "Not applicable unless the change affects user-interface surfaces.\n"
            "<!-- dls:design:end -->",
            "<!-- dls:design:start -->\n"
            "Mode: bypass\n"
            "Rationale: exact existing interaction is already approved.\n"
            "<!-- dls:design:end -->",
        )
        spec.write_text(text, encoding="utf-8")
        commit(self.root, "UI definition")
        projection = decision_projection(self.root, state)
        self.assertTrue(projection["design"]["required"])
        self.assertRegex(projection["design"]["digest"], r"^[0-9a-f]{64}$")

    def test_definition_review_precedes_approval(self) -> None:
        change(self.root, control="standard")
        commit(self.root, "definition")
        projection = decision_projection(self.root, load_state(self.root, "C001"))
        with self.assertRaises(IntegrityError):
            approve(
                self.root,
                change_id="C001",
                decision="definition",
                include_design=False,
                include_architecture=False,
                actor="user",
                response=projection["definition"]["digest"][:12],
                git_sha=git(self.root, "rev-parse", "HEAD"),
                dry_run=False,
            )
        self.assertIsNone(load_state(self.root, "C001")["candidate"])

    def test_exact_head_evidence_and_invalidation(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            state = load_state(self.root, "C001")
            self.assertEqual(git(self.root, "rev-parse", "HEAD"), state["candidate"]["head_sha"])
            self.assertEqual("pass", state["candidate"]["evidence"][0]["status"])
            (self.root / "src.py").write_text("value = 2\n", encoding="utf-8")
            commit(self.root, "new head")
            self.assertEqual("run-candidate-ready", status(self.root, "C001")["next_action"]["id"])
        finally:
            restore_environment(previous)

    def test_descendant_candidate_reuses_preserved_base_and_rejects_conflict(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            first_head = git(self.root, "rev-parse", "HEAD")
            (self.root / "src.py").write_text("value = 2\n", encoding="utf-8")
            commit(self.root, "candidate correction")
            refreshed = candidate_ready(
                self.root,
                change_id="C001",
                base=None,
                addressed=[],
                noted=[],
                dry_run=False,
            )
            pack = json.loads((self.root / refreshed["review_pack_path"]).read_text())
            self.assertEqual(self.base, pack["base_sha"])
            with self.assertRaisesRegex(IntegrityError, "conflicts with the preserved"):
                candidate_ready(
                    self.root,
                    change_id="C001",
                    base=first_head,
                    addressed=[],
                    noted=[],
                    dry_run=False,
                )
        finally:
            restore_environment(previous)

    def test_profile_drift_invalidates_candidate(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            first = load_state(self.root, "C001")["candidate"]["review_id"]
            profiles = self.root / ".dls" / "profiles"
            profiles.mkdir(parents=True)
            (profiles / "local.toml").write_text(
                'schema_version = 1\nname = "local"\nextends = "generic"\n',
                encoding="utf-8",
            )
            config = self.root / ".dls" / "config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'default_profile = "generic"',
                    'default_profile = "local"',
                ),
                encoding="utf-8",
            )
            self.assertEqual("run-candidate-ready", status(self.root, "C001")["next_action"]["id"])
            refreshed = candidate_ready(
                self.root,
                change_id="C001",
                base=self.base,
                addressed=[],
                noted=[],
                dry_run=False,
            )
            self.assertNotEqual(first, refreshed["review_id"])
        finally:
            restore_environment(previous)

    def test_routine_and_standard_use_one_code_analysis(self) -> None:
        for control in ("routine", "standard"):
            with self.subTest(control=control):
                if control == "standard":
                    self.tearDown()
                    self.setUp()
                log, previous = self._prepare_code(control=control)
                try:
                    before = len(self._calls(log))
                    result = review_run(self.root, change_id="C001", kind="code")
                    self.assertEqual("review-clear", result["verdict"])
                    self.assertEqual(1, len(self._calls(log)) - before)
                finally:
                    restore_environment(previous)

    def test_critical_routing_is_deterministic(self) -> None:
        log, previous = self._prepare_code(
            control="critical",
            impacts=["architecture"],
            architecture=True,
        )
        try:
            before = len(self._calls(log))
            review_run(self.root, change_id="C001", kind="code")
            self.assertEqual(1, len(self._calls(log)) - before)
        finally:
            restore_environment(previous)

    def test_critical_trust_risk_uses_two_reviewers(self) -> None:
        log, previous = self._prepare_code(control="critical", impacts=["auth"])
        try:
            before = len(self._calls(log))
            review_run(self.root, change_id="C001", kind="code")
            calls = [json.loads(line) for line in self._calls(log)[before:]]
            self.assertEqual(["gpt-5.6-terra", "gpt-5.6-sol"], [item["model"] for item in calls])
        finally:
            restore_environment(previous)

    def test_reconciliation_runs_only_for_direct_conflict_without_checkout(self) -> None:
        _, previous = self._prepare_code(control="critical", impacts=["auth"])
        try:
            fake_codex(self.root, FAKE_CONFLICT)
            log = self.root / ".dls" / "cache" / "fake-bin" / "calls.jsonl"
            before = len(self._calls(str(log)))
            result = review_run(self.root, change_id="C001", kind="code")
            calls = [json.loads(line) for line in self._calls(str(log))[before:]]
            self.assertEqual("not-clear", result["verdict"])
            self.assertEqual(3, len(calls))
            self.assertFalse(calls[-1]["source_visible"])
            self.assertTrue(calls[-1]["reconcile"])
        finally:
            restore_environment(previous)

    def test_reviewer_owns_finding_verification(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            fake = self.root / ".dls" / "cache" / "fake-bin"
            (fake / "mode").write_text("finding", encoding="utf-8")
            first = review_run(self.root, change_id="C001", kind="code")
            self.assertEqual("not-clear", first["verdict"])
            finding_id = next(iter(load_state(self.root, "C001")["findings"]))
            (self.root / "src.py").write_text("value = 2\n", encoding="utf-8")
            commit(self.root, "fix finding")
            (fake / "mode").write_text("clear", encoding="utf-8")
            candidate_ready(
                self.root,
                change_id="C001",
                base=None,
                addressed=[finding_id],
                noted=[],
                dry_run=False,
            )
            self.assertEqual("addressed", load_state(self.root, "C001")["findings"][finding_id]["status"])
            second = review_run(self.root, change_id="C001", kind="code")
            self.assertEqual("review-clear", second["verdict"])
            self.assertEqual({}, load_state(self.root, "C001")["findings"])
        finally:
            restore_environment(previous)

    def test_invalid_semantic_reference_uses_compact_repair_only(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            fake = self.root / ".dls" / "cache" / "fake-bin"
            (fake / "mode").write_text("finding", encoding="utf-8")
            review_run(self.root, change_id="C001", kind="code")
            finding_id = next(iter(load_state(self.root, "C001")["findings"]))
            (self.root / "src.py").write_text("value = 2\n", encoding="utf-8")
            commit(self.root, "fix finding")
            candidate_ready(
                self.root,
                change_id="C001",
                base=None,
                addressed=[finding_id],
                noted=[],
                dry_run=False,
            )
            fake_codex(self.root, FAKE_REPAIR)
            log = fake / "calls.jsonl"
            before = len(self._calls(str(log)))
            result = review_run(self.root, change_id="C001", kind="code")
            calls = [json.loads(line) for line in self._calls(str(log))[before:]]
            self.assertEqual("review-clear", result["verdict"])
            self.assertEqual([False, True], [item["repair"] for item in calls])
            self.assertNotIn("src.py", calls[1]["prompt"])
        finally:
            restore_environment(previous)

    def test_acceptance_is_separate_and_exact_head(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            review_run(self.root, change_id="C001", kind="code")
            state = load_state(self.root, "C001")
            digest = decision_projection(self.root, state)["definition"]["digest"]
            head = git(self.root, "rev-parse", "HEAD")
            accepted = approve(
                self.root,
                change_id="C001",
                decision="accept",
                include_design=False,
                include_architecture=False,
                actor="user",
                response=f"accept definition {digest[:12]}",
                git_sha=head,
                dry_run=False,
            )
            self.assertTrue(accepted["receipt"]["accepted"])
            self.assertEqual("not-evaluated", accepted["receipt"]["release"])
            self.assertEqual("not-evaluated", accepted["receipt"]["production"])
        finally:
            restore_environment(previous)

    def test_same_content_review_is_reused(self) -> None:
        log, previous = self._prepare_code(control="routine")
        try:
            before = len(self._calls(log))
            one = review_run(self.root, change_id="C001", kind="code")
            two = review_run(self.root, change_id="C001", kind="code")
            self.assertEqual(one["review_result_path"], two["review_result_path"])
            self.assertEqual(1, len(self._calls(log)) - before)
        finally:
            restore_environment(previous)

    def test_transport_failure_gets_one_retry(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            retrying = FAKE_CODEX.replace(
                "args=sys.argv[1:]\n",
                "args=sys.argv[1:]\n"
                "attempt=pathlib.Path(__file__).with_name('attempt')\n"
                "count=int(attempt.read_text())+1 if attempt.exists() else 1\n"
                "attempt.write_text(str(count))\n"
                "if count == 1: sys.exit(9)\n",
            )
            executable, _ = fake_codex(self.root, retrying)
            result = review_run(self.root, change_id="C001", kind="code")
            self.assertEqual("review-clear", result["verdict"])
            self.assertEqual("2", executable.with_name("attempt").read_text())
        finally:
            restore_environment(previous)

    def test_transport_diagnostic_redacts_thread_id(self) -> None:
        _, previous = fake_codex(
            self.root,
            '#!/usr/bin/env python3\nprint(\'{"thread_id":"private-id"}\')\nraise SystemExit(9)\n',
        )
        try:
            with self.assertRaises(IntegrityError) as caught:
                _model_call(
                    workspace=self.root,
                    model="gpt-5.6-terra",
                    effort="high",
                    prompt="test",
                    lane_budget=100,
                )
            self.assertIn("<redacted>", str(caught.exception))
            self.assertNotIn("private-id", str(caught.exception))
        finally:
            restore_environment(previous)

    def test_budget_failure_creates_no_review_and_is_not_retried(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            fake_codex(self.root, FAKE_BUDGET)
            log = self.root / ".dls" / "cache" / "fake-bin" / "calls.jsonl"
            before = len(self._calls(str(log)))
            with self.assertRaisesRegex(IntegrityError, "budget"):
                review_run(self.root, change_id="C001", kind="code")
            self.assertIsNone(load_state(self.root, "C001")["review"])
            self.assertEqual(
                "inspect-review-budget",
                status(self.root, "C001")["next_action"]["id"],
            )
            with self.assertRaisesRegex(IntegrityError, "previously failed"):
                review_run(self.root, change_id="C001", kind="code")
            self.assertEqual(1, len(self._calls(str(log))) - before)
        finally:
            restore_environment(previous)

    def test_stream_events_distinguish_running_from_terminal(self) -> None:
        _, previous = self._prepare_code(control="routine")
        events: list[dict] = []
        try:
            review_run(self.root, change_id="C001", kind="code", stream=events.append)
            self.assertEqual(("started", False), (events[0]["event"], events[0]["terminal"]))
            self.assertEqual(("completed", True), (events[-1]["event"], events[-1]["terminal"]))
        finally:
            restore_environment(previous)

    def test_dependency_requires_accepted_head_in_base(self) -> None:
        change(self.root, change_id="A", control="routine")
        change(self.root, change_id="B", control="routine")
        commit(self.root, "definitions")
        self._approve("A")
        self._approve("B")
        dependency_set(self.root, change_id="B", target="A", dry_run=False)
        self.assertEqual("wait-dependency", status(self.root, "B")["next_action"]["id"])

    def test_worktree_identity_survives_move(self) -> None:
        change(self.root, control="standard")
        commit(self.root, "definition")
        target = self.root.parent / f"{self.root.name}-owner"
        moved = self.root.parent / f"{self.root.name}-moved"
        try:
            prepare(
                self.root,
                change_id="C001",
                base="HEAD",
                path=target,
                branch="codex/C001-implementation",
                dry_run=False,
            )
            git(self.root, "worktree", "move", str(target), str(moved))
            self.assertEqual(moved.resolve(), resolve_change_root(self.root, "C001"))
        finally:
            git(self.root, "worktree", "remove", "--force", str(moved))

    def test_prunable_unrelated_worktree_does_not_break_owner_routing(self) -> None:
        change(self.root, control="standard")
        commit(self.root, "definition")
        owner = self.root.parent / f"{self.root.name}-owner"
        stale = self.root.parent / f"{self.root.name}-stale"
        try:
            prepare(
                self.root,
                change_id="C001",
                base="HEAD",
                path=owner,
                branch="codex/C001-implementation",
                dry_run=False,
            )
            git(self.root, "worktree", "add", "--detach", str(stale), "HEAD")
            shutil.rmtree(stale)
            self.assertEqual(owner.resolve(), resolve_change_root(self.root, "C001"))
        finally:
            git(self.root, "worktree", "remove", "--force", str(owner))
            git(self.root, "worktree", "prune")

    def test_single_flight_reports_running(self) -> None:
        change(self.root, control="routine")
        commit(self.root, "definition")
        self._approve("C001")
        ticket_set(self.root, change_id="C001", ticket_id="C001-T01", value="implemented", note=None)
        (self.root / "src.py").write_text("x=1\n", encoding="utf-8")
        commit(self.root, "implementation")
        original = __import__("dls_core.runner", fromlist=["_run_validation"])._run_validation
        entered = threading.Event()
        release = threading.Event()

        def slow(root: Path, command_id: str) -> dict:
            entered.set()
            release.wait(5)
            return original(root, command_id)

        import dls_core.runner as runner

        runner._run_validation = slow
        results: list[dict] = []
        try:
            thread = threading.Thread(
                target=lambda: results.append(
                    candidate_ready(
                        self.root,
                        change_id="C001",
                        base=self.base,
                        addressed=[],
                        noted=[],
                        dry_run=False,
                    )
                )
            )
            thread.start()
            self.assertTrue(entered.wait(2))
            duplicate = candidate_ready(
                self.root,
                change_id="C001",
                base=self.base,
                addressed=[],
                noted=[],
                dry_run=False,
            )
            self.assertEqual("running", duplicate["status"])
            release.set()
            thread.join(5)
            self.assertEqual("completed", results[0]["status"])
        finally:
            runner._run_validation = original

    def test_validation_failure_never_creates_pack(self) -> None:
        configure(self.root, command="import sys; print('no'); sys.exit(7)")
        change(self.root, control="routine")
        commit(self.root, "definition")
        self._approve("C001")
        ticket_set(self.root, change_id="C001", ticket_id="C001-T01", value="implemented", note=None)
        (self.root / "src.py").write_text("x=1\n", encoding="utf-8")
        commit(self.root, "implementation")
        result = candidate_ready(
            self.root,
            change_id="C001",
            base=self.base,
            addressed=[],
            noted=[],
            dry_run=False,
        )
        self.assertEqual("fix-validation", result["next_action"]["id"])
        self.assertIsNone(load_state(self.root, "C001")["candidate"])
        self.assertEqual([], list((self.root / ".dls" / "reviews").rglob("*.json")))

    def test_public_cli_has_only_the_twelve_core_commands(self) -> None:
        parser = build_parser()
        subparsers = next(action for action in parser._actions if action.dest == "command")
        self.assertEqual(
            {
                "init",
                "doctor",
                "new",
                "adopt",
                "upgrade",
                "status",
                "approve",
                "ticket",
                "dependency",
                "candidate-ready",
                "review-run",
                "worktree",
            },
            set(subparsers.choices),
        )

    def test_bounded_process_stops_timeout_and_output_overflow(self) -> None:
        timeout = _run_bounded(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            cwd=self.root,
            environment={},
            timeout_seconds=0.05,
            max_output_bytes=1024,
        )
        overflow = _run_bounded(
            [sys.executable, "-c", "print('x' * 4096)"],
            cwd=self.root,
            environment={},
            timeout_seconds=5,
            max_output_bytes=1024,
        )
        self.assertTrue(timeout["timed_out"])
        self.assertTrue(overflow["overflow"])

    def test_canonical_state_and_review_contain_no_runtime_context(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            result = review_run(self.root, change_id="C001", kind="code")
            state_text = (self.root / ".dls" / "state" / "C001.json").read_text()
            result_text = (self.root / result["review_result_path"]).read_text()
            for forbidden in (
                str(self.root),
                "CODEX_THREAD_ID",
                "operation_id",
                '"prompt"',
            ):
                self.assertNotIn(forbidden, state_text)
                self.assertNotIn(forbidden, result_text)
        finally:
            restore_environment(previous)

    def test_only_model_output_schema_has_a_runtime_consumer(self) -> None:
        schemas = list(
            (Path(__file__).parents[1] / "assets" / "schemas").glob("*.json")
        )
        self.assertEqual(["review-decision.schema.json"], [path.name for path in schemas])
        runner = (Path(__file__).parents[1] / "scripts" / "dls_core" / "runner.py").read_text()
        self.assertIn("review-decision.schema.json", runner)


class UpgradeTests(unittest.TestCase):
    @staticmethod
    def _legacy_migrated_state(
        root: Path,
        change_id: str,
        *,
        accepted: bool = False,
    ) -> tuple[dict, str, str]:
        state = load_state(root, change_id)
        head = git(root, "rev-parse", "HEAD")
        legacy_digest = "a" * 64
        state["approvals"] = [
            {
                "id": f"{change_id}-definition",
                "bundle_id": None,
                "decision": "definition",
                "digest": legacy_digest,
                "git_sha": head,
                "actor": "user",
                "response_digest": None,
                "status": "current",
                "recorded_at": "2026-07-01T00:00:00Z",
            }
        ]
        state["definition_review"] = {
            "review_id": "legacy-approved-definition",
            "verdict": "review-clear",
            "head_sha": head,
            "definition_digest": legacy_digest,
            "decision_digests": {},
            "provenance": "legacy-approved-definition",
        }
        state["migration"] = {
            "from_schema": 1,
            "source_digest": "b" * 64,
            "migrated_at": "2026-07-01T00:00:00Z",
        }
        if accepted:
            state["approvals"].append(
                {
                    "id": f"{change_id}-accept",
                    "bundle_id": None,
                    "decision": "accept",
                    "digest": legacy_digest,
                    "git_sha": head,
                    "actor": "user",
                    "response_digest": None,
                    "status": "current",
                    "recorded_at": "2026-07-01T00:01:00Z",
                }
            )
            state["review"] = {
                "review_id": f"{change_id}-review",
                "kind": "code",
                "head_sha": head,
                "base_sha": head,
                "definition_digest": legacy_digest,
                "verdict": "review-clear",
                "result_path": f".dls/reviews/{change_id}/legacy.json",
                "result_digest": "c" * 64,
                "usage": {},
                "migrated": True,
            }
            state["acceptance"] = f"{change_id}-accept"
            state["phase"] = "accepted"
            state["lifecycle"] = "accepted"
        path = root / ".dls" / "state" / f"{change_id}.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        return state, head, legacy_digest

    def test_v0110_state_rebases_unchanged_legacy_definition_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository(root)
            configure(root)
            change(
                root,
                change_id="A",
                control="standard",
                impacts=["architecture"],
                adr=True,
            )
            commit(root, "definition")
            _, _, legacy_digest = self._legacy_migrated_state(root, "A", accepted=True)
            commit(root, "record legacy acceptance metadata")

            preview = upgrade(root, apply=False)
            self.assertEqual((0, 1), (preview["to_upgrade"], preview["to_repair"]))
            applied = upgrade(root, apply=True)
            self.assertEqual(1, applied["repaired"])

            state = load_state(root, "A")
            current_digest = definition_digest(root, state)
            self.assertNotEqual(legacy_digest, current_digest)
            self.assertEqual(current_digest, state["definition_review"]["definition_digest"])
            self.assertEqual(current_digest, state["review"]["definition_digest"])
            self.assertEqual(
                {current_digest},
                {
                    item["digest"]
                    for item in state["approvals"]
                    if item["decision"] in {"definition", "accept"}
                },
            )
            self.assertEqual("accepted", status(root, "A")["next_action"]["id"])
            self.assertEqual("A-review", status(root, "A")["review_id"])
            again = upgrade(root, apply=False)
            self.assertEqual((1, 0), (again["already_current"], again["to_repair"]))

    def test_legacy_digest_rebase_refuses_real_definition_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository(root)
            configure(root)
            change(root, change_id="A", control="standard")
            commit(root, "approved definition")
            _, _, legacy_digest = self._legacy_migrated_state(root, "A")
            document = root / load_state(root, "A")["change"]["artifacts"]["spec"]["path"]
            document.write_text(document.read_text() + "\nChanged scope.\n", encoding="utf-8")
            commit(root, "changed definition")

            upgrade(root, apply=True)
            state = load_state(root, "A")
            approval = next(item for item in state["approvals"] if item["decision"] == "definition")
            self.assertEqual(legacy_digest, approval["digest"])
            self.assertEqual("source-changed", state["migration"]["definition_digest_rebase_status"])
            self.assertEqual("run-definition-review", status(root, "A")["next_action"]["id"])

    def test_legacy_dependency_digest_follows_safely_rebased_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository(root)
            configure(root)
            change(root, change_id="A", control="standard")
            change(root, change_id="B", control="standard")
            commit(root, "definitions")
            _, _, legacy_digest = self._legacy_migrated_state(root, "A", accepted=True)
            state_b, _, _ = self._legacy_migrated_state(root, "B")
            state_b["dependencies"] = [
                {
                    "change_id": "A",
                    "requires": "accepted-in-base",
                    "target_definition_digest": legacy_digest,
                }
            ]
            (root / ".dls" / "state" / "B.json").write_text(
                json.dumps(state_b), encoding="utf-8"
            )

            upgrade(root, apply=True)
            target = load_state(root, "A")
            dependent = load_state(root, "B")
            dependency = dependent["dependencies"][0]
            self.assertEqual(definition_digest(root, target), dependency["target_definition_digest"])
            self.assertEqual(legacy_digest, dependency["legacy_target_definition_digest"])
            self.assertEqual("continue-implementation", status(root, "B")["next_action"]["id"])

    def test_upgrade_restores_legacy_candidate_base_and_invalidates_wrong_pack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initial_base = repository(root)
            configure(root)
            change(root, change_id="A", control="routine")
            definition_head = commit(root, "definition")
            projection = decision_projection(root, load_state(root, "A"))
            approve(
                root,
                change_id="A",
                decision="definition",
                include_design=False,
                include_architecture=False,
                actor="user",
                response=f"definition {projection['definition']['digest'][:12]}",
                git_sha=definition_head,
                dry_run=False,
            )
            ticket_set(
                root,
                change_id="A",
                ticket_id="A-T01",
                value="implemented",
                note=None,
            )
            (root / "src.py").write_text("value = 1\n", encoding="utf-8")
            head = commit(root, "implementation")
            legacy = {
                "schema_version": 1,
                "candidate_runs": [
                    {
                        "status": "completed",
                        "head_sha": head,
                        "review_base_sha": definition_head,
                    }
                ],
            }
            archive = root / ".dls" / "archive" / "pre-0.11" / "state"
            archive.mkdir(parents=True)
            (archive / "A.json").write_text(json.dumps(legacy), encoding="utf-8")
            state = load_state(root, "A")
            state["migration"] = {
                "from_schema": 1,
                "source_digest": stable_digest(legacy),
                "migrated_at": "2026-07-01T00:00:00Z",
            }
            state["candidate"] = {
                "status": "ready",
                "head_sha": head,
                "base_sha": initial_base,
                "definition_digest": projection["definition"]["digest"],
                "pack_path": ".dls/reviews/A/packs/wrong.json",
                "pack_digest": "f" * 64,
                "review_id": "wrong",
            }
            state["active_run"] = {
                "run_id": "failed-review",
                "kind": "review:code",
                "head_sha": head,
                "contract_digest": "e" * 64,
                "status": "failed",
                "pid": None,
                "error": "Review lane exceeded its token budget",
                "lanes": {},
            }
            state["phase"] = "review"
            state["lifecycle"] = "candidate-ready"
            (root / ".dls" / "state" / "A.json").write_text(
                json.dumps(state), encoding="utf-8"
            )

            self.assertEqual(1, upgrade(root, apply=False)["to_repair"])
            upgrade(root, apply=True)
            repaired = load_state(root, "A")
            self.assertEqual(definition_head, repaired["candidate"]["base_sha"])
            self.assertEqual("stale", repaired["candidate"]["status"])
            self.assertNotIn("pack_path", repaired["candidate"])
            self.assertIsNone(repaired["active_run"])
            self.assertEqual("run-candidate-ready", status(root, "A")["next_action"]["id"])

            ready = candidate_ready(
                root,
                change_id="A",
                base=None,
                addressed=[],
                noted=[],
                dry_run=False,
            )
            pack = json.loads((root / ready["review_pack_path"]).read_text())
            self.assertEqual(definition_head, pack["base_sha"])
            self.assertEqual("open-review-task", ready["next_action"]["id"])

    def test_v1_to_v2_converter_is_idempotent_and_preserves_19_59(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository(root)
            configure(root)
            state_dir = root / ".dls" / "state"
            for index in range(19):
                change_id = f"E{index:02d}"
                document = root / "docs" / f"{change_id}.md"
                document.parent.mkdir(exist_ok=True)
                document.write_text(f"# {change_id}\n", encoding="utf-8")
                dependencies = [
                    {
                        "change_id": f"E{target:02d}",
                        "blocks_stage": "implementation",
                        "requires": "accepted-in-base",
                        "target_definition_digest": "0" * 64,
                    }
                    for target in range(index)
                ][: 59 - sum(min(value, 59) for value in range(index))]
                state = {
                    "schema_version": 1,
                    "state_revision": 1,
                    "change_id": change_id,
                    "slug": change_id.lower(),
                    "work_kind": "feature",
                    "control_level": "standard",
                    "impact_tags": [],
                    "phase": "definition",
                    "lifecycle": "draft",
                    "artifacts": {"change": {"path": f"docs/{change_id}.md"}},
                    "approvals": [],
                    "tickets": {},
                    "dependencies": dependencies,
                    "reviews": [],
                    "candidate_runs": [],
                    "finding_dispositions": [],
                    "evidence": [],
                    "operations": [],
                }
                (state_dir / f"{change_id}.json").write_text(json.dumps(state), encoding="utf-8")
            # Replace the generated triangular count with exactly 59 deterministic edges.
            remaining = 59
            for index in range(18, -1, -1):
                path = state_dir / f"E{index:02d}.json"
                value = json.loads(path.read_text())
                count = min(index, remaining)
                value["dependencies"] = [
                    {
                        "change_id": f"E{target:02d}",
                        "blocks_stage": "implementation",
                        "requires": "accepted-in-base",
                        "target_definition_digest": "0" * 64,
                    }
                    for target in range(count)
                ]
                remaining -= count
                path.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(0, remaining)
            preview = upgrade(root, apply=False)
            self.assertEqual((19, 59), (preview["total_changes"], preview["dependencies"]))
            applied = upgrade(root, apply=True)
            self.assertEqual(19, applied["upgraded"])
            again = upgrade(root, apply=True)
            self.assertEqual(19, again["already_current"])
            self.assertTrue((root / ".dls" / "archive" / "pre-0.11" / "state" / "E00.json").is_file())
            self.assertEqual(2, load_state(root, "E18")["schema_version"])
            marker = root / ".dls" / "upgrade-incomplete"
            marker.write_text("interrupted\n", encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "incomplete"):
                load_state(root, "E18")


if __name__ == "__main__":
    unittest.main()
