from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from dls_core.cli import build_parser, dispatch
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
from dls_core.runner import (
    BUDGETS,
    _codex_argv,
    _conflicts,
    _model_call,
    _prompt,
    _requirements,
    _run_bounded,
    candidate_ready,
    review_run,
)
from dls_core.worktrees import execution_context, prepare, registry_path, resolve_change_root
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

    def test_model_argv_allows_dls_owned_detached_workspace(self) -> None:
        _, previous = self._fake()
        try:
            argv = _codex_argv(
                workspace=self.root,
                model="gpt-5.6-terra",
                effort="high",
                output=self.root / "decision.json",
            )
            self.assertIn("--skip-git-repo-check", argv)
        finally:
            restore_environment(previous)

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
            reviewed = review_run(self.root, change_id="C001", kind="definition")
            card = reviewed["human_decision"]
            self.assertEqual(
                ["definition", "architecture"],
                [item["decision"] for item in card["decisions"]],
            )
            approve(
                self.root,
                change_id="C001",
                decision="definition",
                include_design=False,
                include_architecture=True,
                actor="user",
                response="Да",
                git_sha=None,
                dry_run=False,
                decision_id=card["id"],
            )
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

    def test_human_decision_presentation_is_derived_without_changing_id(self) -> None:
        change(self.root, control="routine", impacts=["user-interface"])
        state = load_state(self.root, "C001")
        source = self.root / state["change"]["artifacts"]["change"]["path"]
        source.write_text(
            source.read_text().replace(
                "Not applicable unless the change affects user-interface surfaces.",
                "Mode: bypass\nRationale: preserve the committed UI precedent.",
            ),
            encoding="utf-8",
        )
        commit(self.root, "UI definition")
        card = status(self.root, "C001")["human_decision"]
        basis = {
            key: card[key]
            for key in (
                "contract",
                "change_id",
                "action",
                "head_sha",
                "review_id",
                "decisions",
            )
        }
        self.assertEqual(stable_digest(basis), card["id"])
        self.assertEqual("Подтверждение решений", card["presentation"]["title"])
        self.assertEqual(
            ["Описание результата", "Дизайн"],
            [item["label"] for item in card["presentation"]["items"]],
        )
        self.assertTrue(card["presentation"]["unchanged_on_no"])

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
            self.assertEqual(
                "run-candidate-ready",
                status(self.root, "C001")["next_action"]["id"],
            )
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
            self.assertEqual(
                "run-candidate-ready",
                status(self.root, "C001")["next_action"]["id"],
            )
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

    def test_status_profile_projection_is_bounded_and_deterministic(self) -> None:
        change(self.root, control="routine")
        profiles = self.root / ".dls" / "profiles"
        profiles.mkdir(parents=True)
        capabilities = [f'"cap-{index}"' for index in range(20)]
        skills = [f'"skill-{index}"' for index in range(20)]
        (profiles / "local.toml").write_text(
            "schema_version = 1\nname = \"local\"\nextends = \"generic\"\n\n"
            "[routing]\n"
            f"domain_capabilities = [{', '.join(capabilities)}]\n"
            f"domain_skills = [{', '.join(skills)}]\n",
            encoding="utf-8",
        )
        config = self.root / ".dls" / "config.toml"
        config.write_text(
            config.read_text().replace('default_profile = "generic"', 'default_profile = "local"'),
            encoding="utf-8",
        )
        one = status(self.root, "C001")["platform_profile"]
        two = status(self.root, "C001")["platform_profile"]
        self.assertEqual(one, two)
        self.assertEqual("local", one["name"])
        self.assertEqual(16, len(one["domain_capabilities"]))
        self.assertEqual(16, len(one["domain_skills"]))
        self.assertGreater(one["omitted_count"], 0)
        self.assertNotIn(str(self.root), json.dumps(one))

    def test_shared_traceability_matrix_is_limited_to_ticket_scope(self) -> None:
        change(self.root, control="routine")
        matrix = self.root / "docs" / "shared-requirements.json"
        matrix.parent.mkdir(parents=True, exist_ok=True)
        matrix.write_text(
            json.dumps(
                {
                    "epics": {
                        "current": {
                            "requirements": {
                                "F-01": {"producerTicket": "C001-T01"},
                            }
                        },
                        "other": {
                            "requirements": {
                                "F-99": {"producerTicket": "OTHER-T01"},
                                "C-42": {"producerTicket": "OTHER-T01"},
                            }
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        def attach(value: dict) -> None:
            value["change"]["requirement_prefixes"] = ["C", "F"]
            value["change"]["artifacts"]["traceability"] = {
                "path": "docs/shared-requirements.json",
                "role": "definition",
                "producer_ticket_scope": ["C001-T01"],
            }

        state = mutate_state(self.root, "C001", attach)
        self.assertEqual(["F-01"], _requirements(self.root, state))

    def test_review_profile_provenance_is_in_result_state_and_metrics(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            reviewed = review_run(self.root, change_id="C001", kind="code")
            result = json.loads((self.root / reviewed["review_result_path"]).read_text())
            state = load_state(self.root, "C001")
            expected = {
                key: status(self.root, "C001")["platform_profile"][key]
                for key in ("contract", "name", "digest")
            }
            self.assertEqual(expected, result["platform_profile"])
            self.assertEqual(expected, state["review"]["platform_profile"])
            self.assertEqual(expected, status(self.root, "C001", details="metrics")["metrics"]["platform_profile"])

            del state["review"]["platform_profile"]
            (self.root / ".dls" / "state" / "C001.json").write_text(
                json.dumps(state, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.assertIsNone(status(self.root, "C001", details="metrics")["metrics"]["platform_profile"])
        finally:
            restore_environment(previous)

    def test_bundled_profile_skill_routing_is_platform_specific(self) -> None:
        plugin = Path(__file__).resolve().parents[1]
        apple = (plugin / "assets" / "profiles" / "apple.toml").read_text()
        backend = (plugin / "assets" / "profiles" / "server-backend.toml").read_text()
        workflow = (plugin / "skills" / "dls-workflow" / "SKILL.md").read_text()
        self.assertIn("swift-accessibility-skill", apple)
        self.assertNotIn("ios-accessibility", apple)
        for apple_only in ("swiftui-design-principles", "swift-accessibility-skill", "App Store"):
            self.assertNotIn(apple_only, backend)
        self.assertIn("platform_profile", workflow)
        self.assertIn("missing advisory skill never blocks delivery", workflow)
        self.assertIn("never add Apple UI, App Store or", workflow)

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

    def test_backend_critical_review_preserves_profile_and_usage(self) -> None:
        config = self.root / ".dls" / "config.toml"
        config.write_text(
            config.read_text().replace(
                'default_profile = "generic"',
                'default_profile = "server-backend"',
            ),
            encoding="utf-8",
        )
        log, previous = self._prepare_code(
            control="critical",
            impacts=["security-privacy"],
        )
        try:
            before = len(self._calls(log))
            reviewed = review_run(self.root, change_id="C001", kind="code")
            self.assertEqual(2, len(self._calls(log)) - before)
            metrics = status(self.root, "C001", details="metrics")["metrics"]
            self.assertEqual("server-backend", metrics["platform_profile"]["name"])
            self.assertGreater(metrics["processed_tokens"], 0)
            result = json.loads((self.root / reviewed["review_result_path"]).read_text())
            self.assertEqual("server-backend", result["platform_profile"]["name"])
        finally:
            restore_environment(previous)

    def test_apple_ui_definition_review_returns_one_combined_card(self) -> None:
        config = self.root / ".dls" / "config.toml"
        config.write_text(
            config.read_text().replace('default_profile = "generic"', 'default_profile = "apple"'),
            encoding="utf-8",
        )
        change(self.root, control="standard", impacts=["user-interface"])
        state = load_state(self.root, "C001")
        spec = self.root / state["change"]["artifacts"]["spec"]["path"]
        spec.write_text(
            spec.read_text().replace(
                "Not applicable unless the change affects user-interface surfaces.",
                "Mode: bypass\nRationale: preserve the committed UI precedent.",
            ),
            encoding="utf-8",
        )
        commit(self.root, "Apple UI definition")
        _, previous = self._fake()
        try:
            reviewed = review_run(self.root, change_id="C001", kind="definition")
            self.assertEqual("review-clear", reviewed["verdict"])
            self.assertEqual(
                ["definition", "design"],
                [item["decision"] for item in reviewed["human_decision"]["decisions"]],
            )
            self.assertEqual(
                "apple",
                status(self.root, "C001", details="metrics")["metrics"]["platform_profile"]["name"],
            )
        finally:
            restore_environment(previous)

    def test_critical_actionable_primary_short_circuits_secondary(self) -> None:
        log, previous = self._prepare_code(control="critical", impacts=["public-api"])
        try:
            fake = self.root / ".dls" / "cache" / "fake-bin"
            (fake / "mode").write_text("finding", encoding="utf-8")
            before = len(self._calls(log))
            result = review_run(self.root, change_id="C001", kind="code")
            self.assertEqual("not-clear", result["verdict"])
            self.assertEqual(1, len(self._calls(log)) - before)
            routing = load_state(self.root, "C001")["review"]["usage"]["routing"]
            self.assertEqual(["primary"], routing["completed"])
            self.assertEqual(
                [{"lane": "secondary", "reason": "actionable-primary"}],
                routing["skipped"],
            )
        finally:
            restore_environment(previous)

    def test_failed_secondary_recovers_actionable_primary_without_model_call(self) -> None:
        log, previous = self._prepare_code(control="critical", impacts=["public-api"])
        try:
            fake = self.root / ".dls" / "cache" / "fake-bin"
            (fake / "mode").write_text("finding", encoding="utf-8")
            state = load_state(self.root, "C001")
            pack_path = self.root / state["candidate"]["pack_path"]
            pack = json.loads(pack_path.read_text(encoding="utf-8"))
            decision, metadata = _model_call(
                workspace=self.root,
                model="gpt-5.6-terra",
                effort="high",
                prompt=_prompt(pack, lane="primary", lens=None),
                lane_budget=BUDGETS["critical"]["primary"],
            )

            def fail_old_run(value: dict) -> None:
                value["active_run"] = {
                    "run_id": "legacy-budget-run",
                    "kind": "review:code",
                    "head_sha": pack["head_sha"],
                    "contract_digest": "a" * 64,
                    "status": "failed",
                    "pid": None,
                    "error": "Review lane exceeded its token budget",
                    "lanes": {
                        "primary": {
                            "status": "completed",
                            "decision": decision,
                            "metadata": metadata,
                            "completed_at": "2026-08-01T00:00:00Z",
                        },
                        "secondary": {
                            "status": "failed",
                            "error": "Review lane exceeded its token budget",
                        },
                    },
                }

            mutate_state(self.root, "C001", fail_old_run)
            before = len(self._calls(log))
            result = review_run(self.root, change_id="C001", kind="code")
            self.assertEqual("not-clear", result["verdict"])
            self.assertEqual(0, len(self._calls(log)) - before)
            routing = load_state(self.root, "C001")["review"]["usage"]["routing"]
            self.assertEqual(
                [{"lane": "primary", "reason": "prior-budget-failure"}],
                routing["recovered"],
            )
        finally:
            restore_environment(previous)

    def test_additive_secondary_finding_does_not_run_reconciliation(self) -> None:
        _, previous = self._prepare_code(control="critical", impacts=["auth"])
        try:
            fake_codex(self.root, FAKE_CONFLICT)
            log = self.root / ".dls" / "cache" / "fake-bin" / "calls.jsonl"
            before = len(self._calls(str(log)))
            result = review_run(self.root, change_id="C001", kind="code")
            calls = [json.loads(line) for line in self._calls(str(log))[before:]]
            self.assertEqual("not-clear", result["verdict"])
            self.assertEqual(2, len(calls))
            self.assertFalse(any(item["reconcile"] for item in calls))
            self.assertEqual(
                ["primary", "secondary"],
                load_state(self.root, "C001")["review"]["usage"]["routing"]["completed"],
            )
            payload = json.loads((self.root / result["review_result_path"]).read_text())
            finding_id = payload["findings"][0]["id"]
            self.assertIn(finding_id, payload["ticket_verdicts"][0]["finding_ids"])
        finally:
            restore_environment(previous)

    def test_prior_finding_disagreement_is_a_direct_conflict(self) -> None:
        base = {
            "findings": [],
            "ticket_verdicts": [],
            "requirement_verdicts": [],
            "prior_finding_verdicts": [
                {
                    "finding_id": "OLD-1",
                    "verdict": "verified",
                    "replacement_finding_id": None,
                    "evidence": ["diff"],
                }
            ],
        }
        other = json.loads(json.dumps(base))
        other["prior_finding_verdicts"][0]["verdict"] = "waived"
        self.assertTrue(_conflicts(base, other))

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

    def test_intermediate_remediation_commit_is_not_a_candidate_boundary(self) -> None:
        log, previous = self._prepare_code(control="routine")
        try:
            fake = self.root / ".dls" / "cache" / "fake-bin"
            (fake / "mode").write_text("finding", encoding="utf-8")
            result = review_run(self.root, change_id="C001", kind="code")
            self.assertEqual("not-clear", result["verdict"])

            def add_findings(value: dict) -> None:
                original = next(iter(value["findings"].values()))
                for index in range(2, 5):
                    finding = json.loads(json.dumps(original))
                    finding["id"] = f"NEW-{index}"
                    value["findings"][finding["id"]] = finding

            mutate_state(self.root, "C001", add_findings)
            calls_before = len(self._calls(log))
            packs = self.root / ".dls" / "reviews" / "C001" / "packs"
            packs_before = len(list(packs.glob("*.json")))

            for index in range(1, 4):
                (self.root / "src.py").write_text(f"value = {index + 1}\n", encoding="utf-8")
                commit(self.root, f"remediation checkpoint {index}")
                self.assertEqual(
                    "continue-implementation",
                    status(self.root, "C001")["next_action"]["id"],
                )
                self.assertEqual(packs_before, len(list(packs.glob("*.json"))))
                self.assertEqual(calls_before, len(self._calls(log)))

            finding_ids = sorted(load_state(self.root, "C001")["findings"])
            ready = candidate_ready(
                self.root,
                change_id="C001",
                base=None,
                addressed=finding_ids,
                noted=[],
                dry_run=False,
            )
            self.assertEqual("open-review-task", ready["next_action"]["id"])
            self.assertEqual(packs_before + 1, len(list(packs.glob("*.json"))))
            self.assertEqual(calls_before, len(self._calls(log)))
        finally:
            restore_environment(previous)

    def test_waived_and_release_only_findings_do_not_hold_candidate_handoff(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            fake = self.root / ".dls" / "cache" / "fake-bin"
            (fake / "mode").write_text("finding", encoding="utf-8")
            review_run(self.root, change_id="C001", kind="code")

            def release_only(value: dict) -> None:
                finding = next(iter(value["findings"].values()))
                finding["blocks"] = ["release", "production"]

            mutate_state(self.root, "C001", release_only)
            (self.root / "src.py").write_text("value = 2\n", encoding="utf-8")
            commit(self.root, "release evidence update")
            self.assertEqual(
                "run-candidate-ready",
                status(self.root, "C001")["next_action"]["id"],
            )

            def waived(value: dict) -> None:
                finding = next(iter(value["findings"].values()))
                finding["blocks"] = ["review", "acceptance"]
                finding["status"] = "waived"

            mutate_state(self.root, "C001", waived)
            self.assertEqual(
                "run-candidate-ready",
                status(self.root, "C001")["next_action"]["id"],
            )
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
            reviewed = review_run(self.root, change_id="C001", kind="code")
            card = reviewed["human_decision"]
            self.assertEqual("Принять результат? Да / Нет.", card["prompt"])
            self.assertEqual("Принятие реализации", card["presentation"]["title"])
            self.assertIn("release и production", card["presentation"]["effect"])
            self.assertEqual(card, status(self.root, "C001")["human_decision"])
            with self.assertRaisesRegex(IntegrityError, "affirmative"):
                approve(
                    self.root,
                    change_id="C001",
                    decision="accept",
                    include_design=False,
                    include_architecture=False,
                    actor="user",
                    response="Нет",
                    git_sha=None,
                    dry_run=False,
                    decision_id=card["id"],
                )
            accepted = approve(
                self.root,
                change_id="C001",
                decision="accept",
                include_design=False,
                include_architecture=False,
                actor="user",
                response="Да",
                git_sha=None,
                dry_run=False,
                decision_id=card["id"],
            )
            retried = approve(
                self.root,
                change_id="C001",
                decision="accept",
                include_design=False,
                include_architecture=False,
                actor="user",
                response="Да",
                git_sha=None,
                dry_run=False,
                decision_id=card["id"],
            )
            self.assertTrue(accepted["receipt"]["accepted"])
            self.assertEqual(accepted["state_revision"], retried["state_revision"])
            self.assertEqual(card["id"], accepted["approvals"][0]["human_decision_id"])
            self.assertEqual(1, len([item for item in load_state(self.root, "C001")["approvals"] if item["decision"] == "accept"]))
            self.assertEqual("not-evaluated", accepted["receipt"]["release"])
            self.assertEqual("not-evaluated", accepted["receipt"]["production"])
        finally:
            restore_environment(previous)

    def test_stale_human_decision_cannot_accept_new_head(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            card = review_run(self.root, change_id="C001", kind="code")["human_decision"]
            (self.root / "src.py").write_text("value = 2\n", encoding="utf-8")
            commit(self.root, "new candidate")
            with self.assertRaises(IntegrityError):
                approve(
                    self.root,
                    change_id="C001",
                    decision="accept",
                    include_design=False,
                    include_architecture=False,
                    actor="user",
                    response="Да",
                    git_sha=None,
                    dry_run=False,
                    decision_id=card["id"],
                )
            self.assertIsNone(load_state(self.root, "C001")["acceptance"])
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

    def test_lane_budget_is_an_allocation_and_preserves_valid_result(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            fake_codex(self.root, FAKE_BUDGET)
            log = self.root / ".dls" / "cache" / "fake-bin" / "calls.jsonl"
            before = len(self._calls(str(log)))
            result = review_run(self.root, change_id="C001", kind="code")
            self.assertEqual("review-clear", result["verdict"])
            review = load_state(self.root, "C001")["review"]
            self.assertTrue(review["usage"]["reviewers"][0]["budget"]["over_target"])
            self.assertFalse(review["usage"]["routing"]["budget"]["over_target"])
            self.assertEqual(1, len(self._calls(str(log))) - before)
        finally:
            restore_environment(previous)

    def test_clean_aggregate_overrun_cannot_create_review_clear(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            fake_codex(self.root, FAKE_BUDGET.replace("700000", "800000"))
            log = self.root / ".dls" / "cache" / "fake-bin" / "calls.jsonl"
            before = len(self._calls(str(log)))
            with self.assertRaisesRegex(IntegrityError, "aggregate budget"):
                review_run(self.root, change_id="C001", kind="code")
            state = load_state(self.root, "C001")
            self.assertIsNone(state["review"])
            self.assertEqual("review-budget-exhausted", status(self.root, "C001")["next_action"]["id"])
            primary = state["active_run"]["lanes"]["primary"]
            self.assertEqual(800001, primary["metadata"]["usage"]["processed_tokens"])
            with self.assertRaisesRegex(IntegrityError, "aggregate budget"):
                review_run(self.root, change_id="C001", kind="code")
            self.assertEqual(1, len(self._calls(str(log))) - before)
        finally:
            restore_environment(previous)

    def test_actionable_aggregate_overrun_imports_safe_not_clear(self) -> None:
        _, previous = self._prepare_code(control="routine")
        try:
            executable, _ = fake_codex(self.root, FAKE_BUDGET.replace("700000", "800000"))
            executable.with_name("mode").write_text("finding", encoding="utf-8")
            result = review_run(self.root, change_id="C001", kind="code")
            self.assertEqual("not-clear", result["verdict"])
            routing = load_state(self.root, "C001")["review"]["usage"]["routing"]
            self.assertTrue(routing["budget"]["over_target"])
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

    def test_workflow_skill_polls_nested_review_session(self) -> None:
        plugin = Path(__file__).resolve().parents[1]
        skill = (plugin / "skills" / "dls-workflow" / "SKILL.md").read_text()
        cli = (plugin / "skills" / "dls-workflow" / "references" / "cli.md").read_text()
        self.assertIn("tools.write_stdin", skill)
        self.assertIn("while (result.session_id)", cli)
        self.assertIn("tools.write_stdin", cli)
        self.assertIn("Never print only `result.output`", cli)
        self.assertIn("Принять результат? Да / Нет.", skill)
        self.assertIn("Before reading or changing product files", skill)
        self.assertIn("Use `owner_root` as the working directory", skill)
        self.assertIn("worktree prepare\n  CHANGE_ID", skill)
        self.assertIn("immediately imports canonical `not-clear`", skill)
        self.assertIn("intermediate remediation commit is a checkpoint", skill)
        self.assertIn("Never use `--note` for unfinished work", skill)
        self.assertIn("as non-terminal", skill)
        self.assertIn("never send a progress-only final response", skill)
        self.assertIn("After each checkpoint commit, read `status` again", skill)
        self.assertIn("End an implementation task only at `open-review-task`", skill)
        self.assertIn("The bundled `Stop` guard enforces this boundary", skill)
        self.assertIn("dls-auto-continuation-exhausted", skill)
        self.assertIn("absolute per-activation limit", skill)
        self.assertIn("edits, commits,\n  reverts and state changes never reset it", skill)
        self.assertIn("dls-hook-upgrade-required", skill)
        self.assertIn("an already-open task keeps its old skill", skill)
        implementation = skill.split("## Implementation and remediation", 1)[1].split(
            "## Independent review", 1
        )[0]
        self.assertNotIn("review-run", implementation)
        self.assertNotIn("Existing worktree branch does not match requested branch", skill)
        self.assertNotIn("ask the user to accept the exact reviewed HEAD", skill)

    def test_workflow_skill_requires_single_explicit_dirty_draft_handoff(self) -> None:
        plugin = Path(__file__).resolve().parents[1]
        skill = (plugin / "skills" / "dls-workflow" / "SKILL.md").read_text()
        cli = (plugin / "skills" / "dls-workflow" / "references" / "cli.md").read_text()
        prompt = "Продолжить существующий черновик? Да / Нет."
        self.assertEqual(1, skill.count(prompt))
        self.assertIn("immediately following user response is `Да`", skill)
        self.assertIn("without asking again for that draft", skill)
        self.assertIn("Draft permission authorizes continuation only", skill)
        self.assertIn(prompt, cli)
        self.assertIn("preserve the existing diff and continue", cli)

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

    def test_execution_context_prepares_owner_and_leaves_dirty_caller_untouched(self) -> None:
        change(self.root, control="standard")
        commit(self.root, "definition")
        _, previous = self._fake()
        owner = self.root.parent / f"{self.root.name}-owner"
        try:
            review_run(self.root, change_id="C001", kind="definition")
            self._approve("C001")
            resolved, before = execution_context(self.root, "C001")
            self.assertEqual(self.root.resolve(), resolved)
            self.assertEqual("prepare-owner-worktree", before["action"])
            arguments = build_parser().parse_args(
                ["--root", str(self.root), "status", "C001"]
            )
            status_payload = dispatch(arguments)
            self.assertEqual(
                "prepare-owner-worktree", status_payload["next_action"]["id"]
            )
            self.assertEqual(
                "dls-execution-context/v1",
                status_payload["execution_context"]["contract"],
            )

            prepared = prepare(
                self.root,
                change_id="C001",
                base=None,
                path=owner,
                branch="codex/custom-owner-name",
                dry_run=False,
            )
            self.assertEqual(git(self.root, "rev-parse", "HEAD"), prepared["base_sha"])
            original = (self.root / "README.md").read_text(encoding="utf-8")
            (self.root / "README.md").write_text(original + "dirty caller\n", encoding="utf-8")
            resolved, context = execution_context(self.root, "C001")
            self.assertEqual(owner.resolve(), resolved)
            self.assertEqual("ready", context["status"])
            self.assertTrue(context["caller_dirty"])
            self.assertFalse(context["owner_dirty"])
            self.assertEqual("# Fixture\n", (owner / "README.md").read_text(encoding="utf-8"))
        finally:
            restore_environment(previous)
            git(self.root, "worktree", "remove", "--force", str(owner))

    def test_existing_git_worktree_is_bound_without_branch_name_match(self) -> None:
        change(self.root, control="standard")
        commit(self.root, "definition")
        target = self.root.parent / f"{self.root.name}-owner"
        try:
            git(self.root, "worktree", "add", "-b", "codex/unrelated-name", str(target), "HEAD")
            result = prepare(
                self.root,
                change_id="C001",
                base=None,
                path=target,
                branch="codex/C001-implementation",
                dry_run=False,
            )
            self.assertFalse(result["created"])
            self.assertEqual(target.resolve(), resolve_change_root(self.root, "C001"))
        finally:
            git(self.root, "worktree", "remove", "--force", str(target))

    def test_dirty_main_routes_candidate_and_review_to_clean_owner(self) -> None:
        change(self.root, control="standard")
        commit(self.root, "definition")
        executable, previous = self._fake()
        owner = self.root.parent / f"{self.root.name}-owner"
        try:
            self.assertEqual(
                "review-clear",
                review_run(self.root, change_id="C001", kind="definition")["verdict"],
            )
            self._approve("C001")
            prepare(
                self.root,
                change_id="C001",
                base=None,
                path=owner,
                branch="codex/custom-owner-name",
                dry_run=False,
            )
            ticket_set(
                owner,
                change_id="C001",
                ticket_id="C001-T01",
                value="implemented",
                note=None,
            )
            (owner / "src.py").write_text("value = 1\n", encoding="utf-8")
            commit(owner, "implementation")
            (self.root / "README.md").write_text("dirty caller\n", encoding="utf-8")

            ready = dispatch(
                build_parser().parse_args(
                    [
                        "--root",
                        str(self.root),
                        "candidate-ready",
                        "C001",
                        "--base",
                        self.base,
                    ]
                )
            )
            self.assertEqual("open-review-task", ready["next_action"]["id"])
            self.assertEqual(
                git(owner, "rev-parse", "HEAD"),
                load_state(owner, "C001")["candidate"]["head_sha"],
            )

            before = len(self._calls(str(executable.with_name("calls.jsonl"))))
            reviewed = dispatch(
                build_parser().parse_args(
                    ["--root", str(self.root), "review-run", "C001", "--kind", "code"]
                )
            )
            self.assertEqual("review-clear", reviewed["verdict"])
            self.assertEqual(1, len(self._calls(str(executable.with_name("calls.jsonl")))) - before)
            self.assertEqual("dirty caller\n", (self.root / "README.md").read_text())
        finally:
            restore_environment(previous)
            git(self.root, "worktree", "remove", "--force", str(owner))

    def test_prepared_candidate_pack_follows_new_owner(self) -> None:
        change(self.root, control="routine")
        commit(self.root, "definition")
        self._approve("C001")
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
        pack_path = ready["review_pack_path"]
        owner = self.root.parent / f"{self.root.name}-owner"
        try:
            prepare(
                self.root,
                change_id="C001",
                base=None,
                path=owner,
                branch="codex/C001-implementation",
                dry_run=False,
            )
            self.assertEqual(
                (self.root / pack_path).read_bytes(),
                (owner / pack_path).read_bytes(),
            )
        finally:
            git(self.root, "worktree", "remove", "--force", str(owner))

    def test_unique_git_worktree_recovers_missing_registry_binding(self) -> None:
        change(self.root, control="standard")
        commit(self.root, "definition")
        owner = self.root.parent / f"{self.root.name}-owner"
        try:
            prepare(
                self.root,
                change_id="C001",
                base=None,
                path=owner,
                branch="codex/C001-implementation",
                dry_run=False,
            )
            registry_path(self.root).unlink()
            resolved, context = execution_context(self.root, "C001")
            self.assertEqual(owner.resolve(), resolved)
            self.assertEqual("bind-owner-worktree", context["action"])
            recovered = prepare(
                self.root,
                change_id="C001",
                base=None,
                path=None,
                branch=None,
                dry_run=False,
            )
            self.assertTrue(recovered["binding_recovered"])
            _, bound = execution_context(self.root, "C001")
            self.assertTrue(bound["registry_bound"])
        finally:
            git(self.root, "worktree", "remove", "--force", str(owner))

    def test_dirty_owner_stops_before_product_work(self) -> None:
        change(self.root, control="standard")
        commit(self.root, "definition")
        owner = self.root.parent / f"{self.root.name}-owner"
        try:
            prepare(
                self.root,
                change_id="C001",
                base=None,
                path=owner,
                branch="codex/C001-implementation",
                dry_run=False,
            )
            (owner / "README.md").write_text("dirty owner\n", encoding="utf-8")
            resolved, context = execution_context(self.root, "C001")
            self.assertEqual(owner.resolve(), resolved)
            self.assertEqual("conflict", context["status"])
            self.assertEqual("commit-owner-source", context["action"])
        finally:
            git(self.root, "worktree", "remove", "--force", str(owner))

    def test_second_state_bearing_owner_is_an_explicit_conflict(self) -> None:
        change(self.root, control="standard")
        commit(self.root, "definition")
        owner = self.root.parent / f"{self.root.name}-owner"
        duplicate = self.root.parent / f"{self.root.name}-duplicate"
        try:
            prepare(
                self.root,
                change_id="C001",
                base=None,
                path=owner,
                branch="codex/C001-implementation",
                dry_run=False,
            )
            git(self.root, "worktree", "add", "--detach", str(duplicate), "HEAD")
            duplicate_state = duplicate / ".dls" / "state"
            duplicate_state.mkdir(parents=True, exist_ok=True)
            shutil.copy2(
                owner / ".dls" / "state" / "C001.json",
                duplicate_state / "C001.json",
            )
            registry_path(self.root).unlink()
            resolved, context = execution_context(self.root, "C001")
            self.assertIsNone(resolved)
            self.assertEqual("ambiguous-owner", context["reason"])
            self.assertEqual("resolve-owner-conflict", context["action"])
        finally:
            git(self.root, "worktree", "remove", "--force", str(duplicate))
            git(self.root, "worktree", "remove", "--force", str(owner))

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
