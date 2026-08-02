#!/usr/bin/env python3
"""Validate the public Easy Peasy DLS repository without external dependencies."""

from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "dls"
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
MODEL_OUTPUT_SCHEMAS = (
    PLUGIN / "assets" / "schemas" / "review-decision.schema.json",
)
EVALUATION_CLAIM_MAP = ROOT / "docs" / "evaluation-claim-map.md"
EVALUATION_DECISIONS = ROOT / "docs" / "evaluation-decisions.md"
M2_CASES_DOCUMENT = ROOT / "docs" / "evaluation-m2-cases.md"
M2_RUNBOOK_DOCUMENT = ROOT / "docs" / "evaluation-m2-runbook.md"
M2_DECISIONS_DOCUMENT = ROOT / "docs" / "evaluation-m2-decisions.md"

REQUIRED_FILES = (
    ROOT / "README.md",
    ROOT / "LICENSE",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "docs" / "capability-catalog.md",
    ROOT / "docs" / "roadmap.md",
    MARKETPLACE,
    MANIFEST,
    PLUGIN / "hooks" / "hooks.json",
    PLUGIN / "hooks" / "task_guard.py",
    PLUGIN / "skills" / "dls-workflow" / "SKILL.md",
    PLUGIN / "skills" / "dls-debug" / "SKILL.md",
    EVALUATION_CLAIM_MAP,
    EVALUATION_DECISIONS,
    M2_CASES_DOCUMENT,
    M2_RUNBOOK_DOCUMENT,
    M2_DECISIONS_DOCUMENT,
)

FORBIDDEN_PATH_PARTS = {
    ".DS_Store",
    "__pycache__",
    ".dls",
    "promts_v1",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".so", ".dylib"}
FORBIDDEN_TEXT = (
    "/" + "Users/",
    "a." + "burlakov",
    "my-ai-" + "dls",
    "dls-" + "local",
    "op" + "1.md",
    "op" + "2.md",
    "github_" + "pat_",
    "-----BEGIN " + "PRIVATE KEY-----",
)
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)

EVALUATION_CLAIMS = {
    "HC-01": (
        "Before/after state digest is identical",
        (
            "test_core_reset_v011.CoreResetTests.test_acceptance_is_separate_and_exact_head",
            "test_core_reset_v011.CoreResetTests.test_stale_human_decision_cannot_accept_new_head",
        ),
    ),
    "HC-02": (
        "Caller/foreign worktree diff is identical",
        (
            "test_core_reset_v011.CoreResetTests.test_execution_context_prepares_owner_and_leaves_dirty_caller_untouched",
            "test_core_reset_v011.CoreResetTests.test_dirty_main_routes_candidate_and_review_to_clean_owner",
            "test_core_reset_v011.CoreResetTests.test_dirty_owner_stops_before_product_work",
            "test_core_reset_v011.CoreResetTests.test_second_state_bearing_owner_is_an_explicit_conflict",
        ),
    ),
    "HC-03": (
        "HEAD/tree/policy/profile digests match",
        (
            "test_core_reset_v011.CoreResetTests.test_exact_head_evidence_and_invalidation",
            "test_core_reset_v011.CoreResetTests.test_descendant_candidate_reuses_preserved_base_and_rejects_conflict",
            "test_core_reset_v011.CoreResetTests.test_profile_drift_invalidates_candidate",
            "test_core_reset_v011.CoreResetTests.test_validation_failure_never_creates_pack",
        ),
    ),
    "HC-04": (
        "terminal=true and review_result_path != null",
        (
            "test_core_reset_v011.CoreResetTests.test_stream_events_distinguish_running_from_terminal",
        ),
    ),
    "HC-05A": (
        "No bypass; continuation count <= contract",
        (
            "test_task_guard.TaskGuardTests.test_dirty_owner_consent_yes_rearms_guard",
            "test_task_guard.TaskGuardTests.test_dirty_owner_consent_no_clears_guard",
            "test_task_guard.TaskGuardTests.test_changed_draft_does_not_reuse_stale_consent",
        ),
    ),
    "HC-05B": (
        "No bypass; continuation count <= contract",
        (
            "test_task_guard.TaskGuardTests.test_two_continuations_then_terminal_bounded_diagnostic",
            "test_task_guard.TaskGuardTests.test_git_churn_never_resets_absolute_budget",
            "test_task_guard.TaskGuardTests.test_real_progress_does_not_expand_absolute_budget",
        ),
    ),
}

DECISION_LOG_HEADERS = (
    "Date",
    "Component",
    "Claim",
    "Exact version/HEAD",
    "Baseline",
    "Arm-manifest digest",
    "Cases",
    "Result",
    "Safety",
    "Cost/human delta",
    "Decision",
    "Next trigger",
    "Privacy/retention",
)
SYNTHETIC_DECISION_VALUES = (
    "2026-08-02",
    "DLS L0",
    "decision-log-format",
    "synthetic:format-check-v1",
    "not-applicable-l0",
    "not-applicable-l0",
    "synthetic-m1-format-01",
    "passed",
    "not-evaluated",
    "not-measured",
    "format-validated-not-m1-exit",
    "EF-01 accepted receipt",
    "no-private-artifact",
)
DECISION_LOG_CONTENT = "\n".join(
    (
        "# Evaluation decisions",
        "",
        "| " + " | ".join(DECISION_LOG_HEADERS) + " |",
        "|" + "|".join("---" for _ in DECISION_LOG_HEADERS) + "|",
        "| " + " | ".join(SYNTHETIC_DECISION_VALUES) + " |",
        "",
    )
)


def fail(message: str) -> None:
    raise ValueError(message)


def repository_files() -> list[Path]:
    def working_tree_files() -> list[Path]:
        return [
            path.relative_to(ROOT)
            for path in ROOT.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and "__pycache__" not in path.parts
            and path.name != ".DS_Store"
            and path.suffix not in FORBIDDEN_SUFFIXES
        ]

    try:
        output = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        return working_tree_files()
    tracked = {Path(item.decode()) for item in output.split(b"\0") if item}
    return sorted(tracked | set(working_tree_files()))


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"Некорректный JSON {path.relative_to(ROOT)}: {error}")


def validate_required_files() -> None:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_FILES if not path.is_file()]
    if missing:
        fail(f"Отсутствуют обязательные файлы: {', '.join(missing)}")


def validate_marketplace() -> None:
    marketplace = load_json(MARKETPLACE)
    if not isinstance(marketplace, dict):
        fail("Marketplace manifest должен быть JSON object")
    if marketplace.get("name") != "easy-peasy-dls":
        fail("Marketplace name должен быть easy-peasy-dls")
    if marketplace.get("interface", {}).get("displayName") != "Easy Peasy DLS":
        fail("Marketplace displayName должен быть Easy Peasy DLS")
    plugins = marketplace.get("plugins")
    if not isinstance(plugins, list) or len(plugins) != 1:
        fail("Marketplace должен содержать ровно один plugin")
    plugin = plugins[0]
    expected = {
        "name": "dls",
        "source": {"source": "local", "path": "./plugins/dls"},
        "policy": {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        },
        "category": "Productivity",
    }
    if plugin != expected:
        fail("Marketplace entry не соответствует публичному DLS contract")


def validate_plugin_manifest() -> None:
    manifest = load_json(MANIFEST)
    if not isinstance(manifest, dict):
        fail("Plugin manifest должен быть JSON object")
    if manifest.get("name") != "dls":
        fail("Plugin ID должен оставаться dls")
    version = manifest.get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        fail("Plugin version должна быть strict semver")
    if manifest.get("license") != "MIT":
        fail("Plugin license должна быть MIT")
    if manifest.get("repository") != "https://github.com/alexivengo/easy-peasy-dls":
        fail("Plugin repository URL некорректен")
    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        fail("Plugin interface отсутствует")
    if interface.get("displayName") != "Easy Peasy DLS":
        fail("Plugin displayName должен быть Easy Peasy DLS")
    prompts = interface.get("defaultPrompt")
    if (
        not isinstance(prompts, list)
        or not 1 <= len(prompts) <= 3
        or not all(isinstance(item, str) and len(item) <= 128 for item in prompts)
    ):
        fail("defaultPrompt должен содержать от одного до трёх коротких prompts")


def validate_plugin_hooks() -> None:
    path = PLUGIN / "hooks" / "hooks.json"
    config = load_json(path)
    hooks = config.get("hooks") if isinstance(config, dict) else None
    if not isinstance(hooks, dict) or set(hooks) != {"UserPromptSubmit", "Stop"}:
        fail("DLS hooks должны содержать только UserPromptSubmit и Stop")
    for event, groups in hooks.items():
        if not isinstance(groups, list) or len(groups) != 1:
            fail(f"Hook {event} должен иметь одну matcher group")
        handlers = groups[0].get("hooks") if isinstance(groups[0], dict) else None
        if not isinstance(handlers, list) or len(handlers) != 1:
            fail(f"Hook {event} должен иметь один handler")
        handler = handlers[0]
        command = handler.get("command") if isinstance(handler, dict) else None
        if (
            handler.get("type") != "command"
            or not isinstance(command, str)
            or not command.startswith("python3 -c ")
            or "${PLUGIN_ROOT}/hooks/task_guard.py" not in command
            or "os.execv" not in command
            or "dls-hook-upgrade-required" not in command
            or "/" + "Users/" in command
        ):
            fail(
                f"Hook {event} должен использовать upgrade-safe plugin-local "
                "task_guard bootstrap"
            )


def validate_json_files() -> None:
    for path in sorted(ROOT.rglob("*.json")):
        if ".git" not in path.parts:
            load_json(path)


def validate_strict_output_schema(value: object, *, location: str = "$") -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_strict_output_schema(item, location=f"{location}[{index}]")
        return
    if not isinstance(value, dict):
        return
    properties = value.get("properties")
    if isinstance(properties, dict):
        required = value.get("required")
        if not isinstance(required, list) or set(required) != set(properties):
            fail(
                f"Structured-output schema {location}: required должен точно "
                "совпадать с properties"
            )
        if value.get("additionalProperties") is not False:
            fail(
                f"Structured-output schema {location}: "
                "additionalProperties должен быть false"
            )
    for key, item in value.items():
        validate_strict_output_schema(item, location=f"{location}.{key}")


def validate_model_output_schemas() -> None:
    for path in MODEL_OUTPUT_SCHEMAS:
        schema = load_json(path)
        validate_strict_output_schema(
            schema,
            location=str(path.relative_to(ROOT)),
        )


def validate_skills() -> None:
    for skill_name in ("dls-workflow", "dls-debug"):
        skill = PLUGIN / "skills" / skill_name / "SKILL.md"
        text = skill.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            fail(f"{skill_name}: отсутствует YAML frontmatter")
        frontmatter = text.split("---", 2)[1]
        if f"name: {skill_name}" not in frontmatter:
            fail(f"{skill_name}: name не совпадает с папкой")
        if "description:" not in frontmatter:
            fail(f"{skill_name}: отсутствует description")


def validate_platform_profiles() -> None:
    sys.path.insert(0, str(PLUGIN / "scripts"))
    try:
        from dls_core.repo import PROFILE_CONTRACT, resolve_profile

        names = {path.stem for path in (PLUGIN / "assets" / "profiles").glob("*.toml")}
        if names != {"generic", "apple", "server-backend"}:
            fail("Публичный profile set должен содержать generic, apple и server-backend")
        for name in sorted(names):
            profile = resolve_profile(ROOT, config={"default_profile": name})
            if profile.get("contract") != PROFILE_CONTRACT:
                fail(f"Profile {name} использует неизвестный runtime contract")
        backend = resolve_profile(ROOT, config={"default_profile": "server-backend"})
        projection = json.dumps(backend, ensure_ascii=False).lower()
        if "backend-architecture" not in projection or "rollback-drill" not in projection:
            fail("server-backend profile не содержит обязательную backend vocabulary")
        if "app-store" in projection or "swiftui" in projection:
            fail("server-backend profile содержит Apple-only routing")
    finally:
        try:
            sys.path.remove(str(PLUGIN / "scripts"))
        except ValueError:
            pass


def _test_ids(suite: unittest.TestSuite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _test_ids(item)
        else:
            yield item.id()


def discovered_dls_test_ids() -> set[str]:
    scripts = str(PLUGIN / "scripts")
    tests = str(PLUGIN / "tests")
    sys.path[:0] = [scripts, tests]
    try:
        suite = unittest.defaultTestLoader.discover(tests, pattern="test_*.py")
        return set(_test_ids(suite))
    finally:
        for path in (scripts, tests):
            try:
                sys.path.remove(path)
            except ValueError:
                pass


def validate_evaluation_documents() -> None:
    text = EVALUATION_CLAIM_MAP.read_text(encoding="utf-8")
    headings = tuple(re.findall(r"^## (HC-[0-9]+[A-Z]?)$", text, flags=re.MULTILINE))
    if headings != tuple(EVALUATION_CLAIMS):
        fail("Evaluation claim map должен содержать только обязательные HC headings")
    discovered = discovered_dls_test_ids()
    for claim, (oracle, expected_ids) in EVALUATION_CLAIMS.items():
        match = re.search(
            rf"^## {re.escape(claim)}\n(?P<body>.*?)(?=^## |\Z)",
            text,
            flags=re.MULTILINE | re.DOTALL,
        )
        if not match:
            fail(f"Evaluation claim map: отсутствует {claim}")
        lines = [line for line in match.group("body").splitlines() if line]
        expected_lines = [
            f"Hard oracle: {oracle}",
            *[f"- `{test_id}`" for test_id in expected_ids],
        ]
        if lines != expected_lines:
            fail(f"Evaluation claim map: неверная grammar для {claim}")
        missing = [test_id for test_id in expected_ids if test_id not in discovered]
        if missing:
            fail(f"Evaluation claim map: test ID не discoverable: {', '.join(missing)}")
    if EVALUATION_DECISIONS.read_text(encoding="utf-8") != DECISION_LOG_CONTENT:
        fail("Evaluation decisions должен оставаться закрытым M1 seed log")


M2_CASE_IDS = ("SR-01", "SR-02", "SR-03", "SR-04")
M2_CASE_REGISTRY = {
    "SR-01": ("SR-01.current", "not-applicable", "1", "2"),
    "SR-02": ("SR-02.current", "not-applicable", "1", "2"),
    "SR-03": ("SR-03.current", "SR-03.primary-only", "3", "4"),
    "SR-04": ("SR-04.repair", "SR-04.fail-closed", "2", "3"),
}
M2_CASE_FIELDS = (
    "case_id",
    "claim",
    "fixture_sha",
    "tree_digest",
    "task_input_digest",
    "oracle_version",
    "oracle_digest",
    "oracle_owner",
    "custody_digest",
    "repair_boundary_digest",
    "current_manifest_digest",
    "reference_manifest_digest",
    "time_ceiling_seconds",
    "token_ceiling",
    "privacy",
    "custody_retention",
)
M2_RECORD_FIELDS = (
    "case_id",
    "run_state",
    "plugin_version",
    "agent_version",
    "model",
    "effort",
    "run_date",
    "fixture_sha",
    "tree_digest",
    "task_input_digest",
    "oracle_version",
    "oracle_digest",
    "custody_digest",
    "repair_boundary_digest",
    "repair_execution_proof_digest",
    "current_manifest_digest",
    "reference_manifest_digest",
    "processed_tokens",
    "wall_time_seconds",
    "custody_retention",
    "privacy_retention",
)
M2_ARM_HEADER = (
    "Arm",
    "Expected verdict",
    "Expected lanes",
    "Call contract",
    "Permitted manifest difference",
    "Repair access",
    "Actual verdict",
    "Outcome",
    "Hard oracle",
    "Safety violations",
    "Lanes",
    "Attempts",
    "Successful calls",
    "Finding class",
    "Execution receipt",
)
M2_ARMS = {
    "SR-01": (
        ("SR-01.current", "review-clear", "primary", "primary=1;secondary=0;repair=0;transport-retry<=1", "none", "not-applicable"),
    ),
    "SR-02": (
        ("SR-02.current", "not-clear", "primary", "primary=1;secondary=0;repair=0;transport-retry<=1", "none", "not-applicable"),
    ),
    "SR-03": (
        ("SR-03.current", "not-clear", "primary,secondary", "primary=1;secondary=1;repair=0;transport-retry<=1", "none", "not-applicable"),
        ("SR-03.primary-only", "review-clear", "primary", "primary=1;secondary=0;repair=0;transport-retry<=1", "secondary-lane=disabled", "not-applicable"),
    ),
    "SR-04": (
        ("SR-04.repair", "review-clear", "primary", "primary=1;secondary=0;repair=1;transport-retry<=1", "repair-mode=compact", "source-blind:review-output+format-error"),
        ("SR-04.fail-closed", "not-applicable", "none", "primary=0;secondary=0;repair=0;transport-retry<=0", "repair-mode=fail-closed", "not-applicable"),
    ),
}
M2_RUNBOOK = (
    ("Preconditions", (
        ("dependency", "EF-01 accepted-in-base at d4b9e2f57c4061249d6ac346479aedd6149ed24e069f9b9c0552178b86d7b1c5"),
        ("plugin-version", "dls 0.13.6+codex.20260802111333; reinstall or hot reload during an arm invalidates that arm"),
        ("execution-profile", "lock one plugin, agent, model, effort, and same-day run date in every record before manual-m2-arm"),
        ("fresh-task", "a new Codex task starts before the first arm; no restart during an arm"),
        ("source-clean", "the fixture and release-record worktree are clean at arm launch; after recording, validation, and commit they are clean before the next arm"),
        ("manual-m2-arm", "a release-authorized human invokes unchanged review-run --kind code in the declared disposable fixture; this M2 procedure does not restrict ordinary definition/code review"),
        ("record-commit", "write only the decision record in a dedicated release-record worktree after arm cleanup; validate and commit it before the next arm"),
    )),
    ("Custody and locks", (
        ("custody-bundle", "one immutable private bundle per case with fixture recipe, fixed Git metadata, hidden oracle, per-arm execution receipts, SR-04 boundary receipt, and SR-04 execution proof"),
        ("arm-receipt", "every completed arm records an arm-receipt-v1 digest bound to its locks, profile, DLS result, hard oracle, matcher, counters, and actual fields; no raw artifact enters public record"),
        ("source-blind-boundary", "SR-04 repair accepts only prior review output and format error in a fresh empty temporary workspace with allowlist-empty environment and read-only sandbox; fixture, task source, hidden oracle, custody, network, and tool access are denied"),
        ("lock-check", "fixture, tree, input, oracle, custody, current/reference manifest, per-arm difference, and SR-04 repair-boundary locks match before a live arm"),
        ("repair-proof", "a completed SR-04.repair records a source-blind-v1 proof digest bound to that arm; the proof carries only digests, empty-temporary workspace, allowlist-empty environment, read-only sandbox, and zero denied reads"),
        ("private-replay", "an authorized evaluator and public validator fixtures use the canonical arm-receipt-v1/source-blind-v1 byte format to reproduce every lock, recompute every arm receipt and the SR-04 proof digest, and reject any mismatch before a clear M2 outcome"),
    )),
    ("Arm order", (
        ("SR-01", "SR-01.current"),
        ("SR-02", "SR-02.current"),
        ("SR-03", "SR-03.current then SR-03.primary-only on the same day"),
        ("SR-04", "SR-04.repair then SR-04.fail-closed on the same day"),
    )),
    ("Attempt accounting", (
        ("attempt-syntax", "primary=n;secondary=n;repair=n;transport-failed=n"),
        ("successful-call-syntax", "primary=n;secondary=n;repair=n"),
        ("sample-budget", "seven nominal calls; at most eight calls across SR-01 through SR-04"),
        ("transport-retry", "one sample-wide retry before a semantic result and within its case and sample ceilings"),
    )),
    ("Stop outcomes", (
        ("hard-gate", "a current safety violation or failed current hard oracle stops that case and makes M2 not-clear"),
        ("invalid-case", "only SR-04.fail-closed is the expected contrast invalid-case; every other invalid-case makes M2 not-clear"),
        ("infrastructure-failed", "missing cumulative meter, transport failure without the permitted retry, or unavailable lock evidence makes M2 not-clear"),
        ("budget-exhausted", "a call that would exceed call, time, or token ceiling is not launched and makes M2 not-clear"),
    )),
    ("Record transition", (
        ("planned", "the planned profile uses not-locked lock placeholders except required not-applicable values; actual arm values and meters not-run; custody retained-for:365d-after-decision"),
        ("locked-not-run", "all locks match the case record; actual arm values not-run; custody retained-for:365d-after-decision"),
        ("completed", "terminal arm values and cumulative meters recorded; every executed arm requires its verified receipt and SR-04.repair its verified proof digest before clear; missing meter is infrastructure-failed"),
        ("aborted", "a stop writes decision_state=aborted and m2_outcome=not-clear; the executed case prefix is retained and all later case records stay unrun"),
        ("decision", "keep/improve/delete only for a clear M2 outcome with useful evidence; otherwise not-applicable"),
    )),
    ("Retention", (
        ("custody-retention", "retain each private bundle through the verified date at least 365 days after the final M2 decision"),
        ("raw-output-retention", "keep raw private output no longer than 30 days or the final decision, whichever is earlier"),
        ("public-record", "public synthetic locks, typed outcomes, counters, and dates only; no path, prompt, transcript, source, session, or secret"),
    )),
)
M2_SHA256 = re.compile(r"sha256:[0-9a-f]{64}$")
M2_GIT_SHA = re.compile(r"git:[0-9a-f]{40}$")
M2_CLAIM = re.compile(r"[a-z][a-z0-9-]{0,63}$")
M2_INTEGER = re.compile(r"0|[1-9][0-9]*$")
M2_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}$")
M2_EXECUTION_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}$")
M2_CANONICAL_RECORD_FIELDS = {
    "arm-receipt-v1": (
        "case_id",
        "arm_id",
        "fixture_sha",
        "tree_digest",
        "task_input_digest",
        "oracle_version",
        "oracle_digest",
        "custody_digest",
        "repair_boundary_digest",
        "current_manifest_digest",
        "reference_manifest_digest",
        "plugin_version",
        "agent_version",
        "model",
        "effort",
        "run_date",
        "dls_result_digest",
        "hard_oracle_evidence_digest",
        "matcher_evidence_digest",
        "actual_verdict",
        "outcome",
        "hard_oracle",
        "safety_violations",
        "lanes",
        "attempts",
        "successful_calls",
        "finding_class",
        "repair_execution_proof_digest",
    ),
    "source-blind-v1": (
        "arm_id",
        "repair_boundary_digest",
        "repair_input_digest",
        "repair_output_digest",
        "transcript_digest",
        "workspace",
        "environment",
        "sandbox",
        "denied_reads",
    ),
}
M2_PRIVACY_PATTERNS = (
    ("P01 path", ("file://", "/" + "Users/", "/home/", "/var/", "/tmp/", "C:\\Users\\", "C:\\home\\", "C:\\var\\", "C:\\tmp\\")),
    ("P04 session identifier", (re.compile(r"(?i)\bsession[_-]?id\s*[:=]"), re.compile(r"(?i)\bthread[_-]?id\s*[:=]"), re.compile(r"(?i)\bconversation[_-]?id\s*[:=]"))),
    ("P05 secret", (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), re.compile(r"ghp_[A-Za-z0-9]{20,}"), re.compile(r"github" + "_" + r"pat_[A-Za-z0-9_]{20,}"), re.compile("-----BEGIN " + "[A-Z ]*PRIVATE KEY-----"))),
    ("P06 private fixture", ("private-fixture", "fixture-source", "hidden-oracle-source", "custody-bundle-path", "raw-prompt", "raw-transcript")),
)


def _m2_fail(assertion: str, message: str) -> None:
    fail(f"{assertion}: {message}")


def m2_canonical_digest(record_type: str, fields: tuple[tuple[str, str], ...]) -> str:
    expected = M2_CANONICAL_RECORD_FIELDS.get(record_type)
    if expected is None or tuple(name for name, _value in fields) != expected:
        _m2_fail("m2-receipt", "canonical record field order")
    if any(not value or not value.isascii() or any(not (" " <= character <= "~") for character in value) for _name, value in fields):
        _m2_fail("m2-receipt", "canonical record value")
    payload = "".join((f"record_type={record_type}\n", *(f"{name}={value}\n" for name, value in fields)))
    return "sha256:" + hashlib.sha256(payload.encode("ascii")).hexdigest()


def _m2_cells(line: str, assertion: str) -> list[str]:
    if not line.startswith("|") or not line.endswith("|"):
        _m2_fail(assertion, "expected Markdown table row")
    return [cell.strip() for cell in line[1:-1].split("|")]


def _m2_table(lines: list[str], index: int, header: tuple[str, ...], assertion: str) -> tuple[list[list[str]], int]:
    if index + 1 >= len(lines) or tuple(_m2_cells(lines[index], assertion)) != header:
        _m2_fail(assertion, "unexpected table header")
    separator = _m2_cells(lines[index + 1], assertion)
    if len(separator) != len(header) or not all(re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
        _m2_fail(assertion, "invalid table separator")
    index += 2
    rows: list[list[str]] = []
    while index < len(lines) and lines[index].startswith("|"):
        row = _m2_cells(lines[index], assertion)
        if len(row) != len(header):
            _m2_fail(assertion, "unexpected table column count")
        rows.append(row)
        index += 1
    if not rows:
        _m2_fail(assertion, "table has no rows")
    return rows, index


def _m2_load(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    raw_lines = text.splitlines()
    for marker, values in M2_PRIVACY_PATTERNS:
        for value in values:
            if (value.search(text) if isinstance(value, re.Pattern) else value in text):
                _m2_fail("m2-privacy", f"{path.name} contains {marker}")
    for line in raw_lines:
        if line.startswith("```"):
            _m2_fail("m2-privacy", f"{path.name} contains P02 fence")
        if re.match(r"(?i)^#{1,6}\s*(prompt|transcript|source|session)\b", line):
            _m2_fail("m2-privacy", f"{path.name} contains P03 artifact heading")
        if line.startswith("diff --git "):
            _m2_fail("m2-privacy", f"{path.name} contains P07 source diff")
        if line and not line.startswith("#") and not line.startswith("|"):
            _m2_fail("m2-privacy", f"{path.name} has text outside headings and tables")
        if re.search(r"<[A-Za-z!/]", line) or "](" in line:
            _m2_fail("m2-privacy", f"{path.name} contains HTML or Markdown link syntax")
    return [line for line in raw_lines if line]


def _m2_fields(rows: list[list[str]], names: tuple[str, ...], assertion: str) -> dict[str, str]:
    found = tuple(row[0] for row in rows)
    if found != names:
        _m2_fail(assertion, "unexpected field names or order")
    return dict(rows)


def _m2_is_integer(value: str, *, positive: bool = False) -> bool:
    return bool(M2_INTEGER.fullmatch(value)) and (not positive or value != "0")


def _m2_is_date(value: str) -> bool:
    if not M2_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _m2_is_digest_or_unlocked(value: str) -> bool:
    return value == "not-locked" or bool(M2_SHA256.fullmatch(value))


def _m2_validate_case_fields(case_id: str, fields: dict[str, str]) -> None:
    if fields["case_id"] != case_id or not M2_CLAIM.fullmatch(fields["claim"]):
        _m2_fail("m2-field-shape", f"{case_id} case identity")
    if fields["fixture_sha"] != "not-locked" and not M2_GIT_SHA.fullmatch(fields["fixture_sha"]):
        _m2_fail("m2-enums", f"{case_id} fixture_sha")
    for name in ("tree_digest", "task_input_digest", "oracle_digest", "custody_digest", "current_manifest_digest"):
        if not _m2_is_digest_or_unlocked(fields[name]):
            _m2_fail("m2-enums", f"{case_id} {name}")
    if fields["oracle_version"] != "not-locked" and not re.fullmatch(r"v[1-9][0-9]*", fields["oracle_version"]):
        _m2_fail("m2-enums", f"{case_id} oracle_version")
    if fields["oracle_owner"] != "dls-maintainer" or fields["privacy"] != "public-synthetic":
        _m2_fail("m2-enums", f"{case_id} required literal")
    reference = fields["reference_manifest_digest"]
    if case_id in {"SR-01", "SR-02"}:
        if reference != "not-applicable":
            _m2_fail("m2-state-transition", f"{case_id} reference manifest")
    elif not _m2_is_digest_or_unlocked(reference):
        _m2_fail("m2-enums", f"{case_id} reference_manifest_digest")
    repair = fields["repair_boundary_digest"]
    if case_id == "SR-04":
        if not _m2_is_digest_or_unlocked(repair):
            _m2_fail("m2-enums", "SR-04 repair_boundary_digest")
    elif repair != "not-applicable":
        _m2_fail("m2-state-transition", f"{case_id} repair_boundary_digest")
    if not _m2_is_integer(fields["time_ceiling_seconds"], positive=True) or not _m2_is_integer(fields["token_ceiling"], positive=True):
        _m2_fail("m2-enums", f"{case_id} ceiling")
    if fields["custody_retention"] != "retained-for:365d-after-decision":
        _m2_fail("m2-enums", f"{case_id} custody_retention")


def _m2_counter(value: str, names: tuple[str, ...], assertion: str) -> dict[str, int]:
    parts = value.split(";")
    if len(parts) != len(names):
        _m2_fail(assertion, "counter field count")
    counters: dict[str, int] = {}
    for part, name in zip(parts, names):
        prefix = f"{name}="
        if not part.startswith(prefix) or not _m2_is_integer(part[len(prefix):]):
            _m2_fail(assertion, "counter syntax")
        counters[name] = int(part[len(prefix):])
    return counters


def _m2_contract(value: str) -> dict[str, int]:
    parts = value.split(";")
    expected = ("primary", "secondary", "repair", "transport-retry<=")
    if len(parts) != len(expected):
        _m2_fail("m2-enums", "call contract field count")
    contract: dict[str, int] = {}
    for part, prefix in zip(parts, expected):
        marker = f"{prefix}=" if not prefix.endswith("<=") else prefix
        if not part.startswith(marker) or not _m2_is_integer(part[len(marker):]):
            _m2_fail("m2-enums", "call contract syntax")
        contract[prefix.rstrip("<=")] = int(part[len(marker):])
    return contract


def _m2_validate_actual(case_id: str, arm: tuple[str, str, str, str, str, str], row: list[str], state: str) -> None:
    actual = row[6:]
    if state in {"planned", "locked-not-run"}:
        if actual != ["not-run"] * 9:
            _m2_fail("m2-state-transition", f"{case_id} pre-live arm values")
        return
    if state != "completed":
        _m2_fail("m2-state-transition", f"{case_id} run_state")
    verdict, outcome, oracle, safety, lanes, attempts, successful, finding, receipt = actual
    if actual == ["not-run"] * 9:
        return
    if outcome == "budget-exhausted":
        if actual != ["not-run", "budget-exhausted", "not-run", "not-run", "not-run", "not-run", "not-run", "not-run", "not-run"]:
            _m2_fail("m2-state-transition", f"{case_id} budget-exhausted arm")
        return
    if not M2_SHA256.fullmatch(receipt):
        _m2_fail("m2-receipt", f"{case_id} arm receipt")
    if verdict not in {"review-clear", "not-clear", "not-applicable"}:
        _m2_fail("m2-enums", f"{case_id} actual verdict")
    if outcome not in {"passed", "product-failed", "component-failed", "infrastructure-failed", "invalid-case", "budget-exhausted"}:
        _m2_fail("m2-enums", f"{case_id} outcome")
    if oracle not in {"passed", "failed"}:
        _m2_fail("m2-enums", f"{case_id} hard oracle")
    is_reference = arm[0] != M2_CASE_REGISTRY[case_id][0]
    if (is_reference and safety != "not-applicable") or (not is_reference and not _m2_is_integer(safety)):
        _m2_fail("m2-enums", f"{case_id} safety violations")
    if lanes not in {"primary", "primary,secondary", "none"}:
        _m2_fail("m2-enums", f"{case_id} lanes")
    attempts_map = _m2_counter(attempts, ("primary", "secondary", "repair", "transport-failed"), "m2-attempt-budget")
    successful_map = _m2_counter(successful, ("primary", "secondary", "repair"), "m2-attempt-budget")
    contract = _m2_contract(arm[3])
    if any(attempts_map[name] > contract[name] for name in ("primary", "secondary", "repair")) or attempts_map["transport-failed"] > contract["transport-retry"]:
        _m2_fail("m2-attempt-budget", f"{case_id} arm ceiling")
    if any(successful_map[name] > attempts_map[name] for name in successful_map):
        _m2_fail("m2-attempt-budget", f"{case_id} successful calls")
    if finding not in {"useful", "noisy", "dangerous-miss", "uncertain", "no-finding", "not-applicable"}:
        _m2_fail("m2-enums", f"{case_id} finding class")
    if outcome == "passed" and (verdict != arm[1] or lanes != arm[2] or oracle != "passed"):
        _m2_fail("m2-overall-outcome", f"{case_id} passed arm contract")


def _m2_arms(case_id: str, rows: list[list[str]], state: str) -> dict[str, list[str]]:
    expected = M2_ARMS[case_id]
    if len(rows) != len(expected):
        _m2_fail("m2-document-order", f"{case_id} arm count")
    found: dict[str, list[str]] = {}
    for row, arm in zip(rows, expected):
        if tuple(row[:6]) != arm:
            _m2_fail("m2-field-shape", f"{case_id} arm contract")
        found[arm[0]] = row
        _m2_validate_actual(case_id, arm, row, state)
    return found


def _m2_cases() -> dict[str, dict[str, object]]:
    lines = _m2_load(M2_CASES_DOCUMENT)
    index = 0
    if not lines or lines[index] != "# M2 frozen cases":
        _m2_fail("m2-document-order", "cases title")
    index += 1
    if index >= len(lines) or lines[index] != "## Case registry":
        _m2_fail("m2-document-order", "case registry heading")
    index += 1
    rows, index = _m2_table(lines, index, ("Case", "Current arm", "Reference arm", "Nominal attempts", "Retry ceiling"), "m2-document-order")
    if [tuple(row) for row in rows] != [(case_id, *M2_CASE_REGISTRY[case_id]) for case_id in M2_CASE_IDS]:
        _m2_fail("m2-document-order", "case registry rows")
    cases: dict[str, dict[str, object]] = {}
    for case_id in M2_CASE_IDS:
        if index >= len(lines) or lines[index] != f"## {case_id}":
            _m2_fail("m2-document-order", f"{case_id} heading")
        index += 1
        if index >= len(lines) or lines[index] != "### Case fields":
            _m2_fail("m2-document-order", f"{case_id} field heading")
        index += 1
        field_rows, index = _m2_table(lines, index, ("Field", "Value"), "m2-field-shape")
        fields = _m2_fields(field_rows, M2_CASE_FIELDS, "m2-field-shape")
        _m2_validate_case_fields(case_id, fields)
        if index >= len(lines) or lines[index] != "### Arm records":
            _m2_fail("m2-document-order", f"{case_id} arm heading")
        index += 1
        arm_rows, index = _m2_table(lines, index, M2_ARM_HEADER, "m2-field-shape")
        arms = _m2_arms(case_id, arm_rows, "planned")
        cases[case_id] = {"fields": fields, "arms": arms}
    if index != len(lines):
        _m2_fail("m2-document-order", "extra cases content")
    return cases


def _m2_runbook() -> None:
    lines = _m2_load(M2_RUNBOOK_DOCUMENT)
    index = 0
    if not lines or lines[index] != "# M2 release runbook":
        _m2_fail("m2-document-order", "runbook title")
    index += 1
    for section, expected_rows in M2_RUNBOOK:
        if index >= len(lines) or lines[index] != f"## {section}":
            _m2_fail("m2-document-order", f"runbook {section}")
        index += 1
        rows, index = _m2_table(lines, index, ("Rule", "Value"), "m2-field-shape")
        if [tuple(row) for row in rows] != list(expected_rows):
            _m2_fail("m2-field-shape", f"runbook {section} rules")
    if index != len(lines):
        _m2_fail("m2-document-order", "extra runbook content")


def _m2_validate_record(case_id: str, fields: dict[str, str], case_fields: dict[str, str]) -> str:
    state = fields["run_state"]
    if fields["case_id"] != case_id or state not in {"planned", "locked-not-run", "completed"}:
        _m2_fail("m2-state-transition", f"{case_id} record state")
    profile_names = ("plugin_version", "agent_version", "model", "effort", "run_date")
    if state == "planned":
        if any(fields[name] != "not-locked" for name in profile_names):
            _m2_fail("m2-state-transition", f"{case_id} planned execution profile")
    elif (
        fields["plugin_version"] != "dls 0.13.6+codex.20260802111333"
        or not M2_EXECUTION_IDENTIFIER.fullmatch(fields["agent_version"])
        or not M2_EXECUTION_IDENTIFIER.fullmatch(fields["model"])
        or fields["effort"] not in {"low", "medium", "high", "xhigh", "max", "ultra"}
        or not _m2_is_date(fields["run_date"])
    ):
        _m2_fail("m2-enums", f"{case_id} execution profile")
    lock_names = ("fixture_sha", "tree_digest", "task_input_digest", "oracle_version", "oracle_digest", "custody_digest", "repair_boundary_digest", "current_manifest_digest", "reference_manifest_digest")
    if state == "planned":
        expected = {
            "fixture_sha": "not-locked",
            "tree_digest": "not-locked",
            "task_input_digest": "not-locked",
            "oracle_version": "not-locked",
            "oracle_digest": "not-locked",
            "custody_digest": "not-locked",
            "repair_boundary_digest": "not-applicable" if case_id != "SR-04" else "not-locked",
            "current_manifest_digest": "not-locked",
            "reference_manifest_digest": "not-applicable" if case_id in {"SR-01", "SR-02"} else "not-locked",
        }
        if {name: fields[name] for name in lock_names} != expected:
            _m2_fail("m2-state-transition", f"{case_id} planned lock profile")
    else:
        expected = {name: case_fields[name] for name in lock_names}
        if {name: fields[name] for name in lock_names} != expected or "not-locked" in expected.values():
            _m2_fail("m2-state-transition", f"{case_id} locked record")
    proof = fields["repair_execution_proof_digest"]
    if case_id != "SR-04" and proof != "not-applicable":
        _m2_fail("m2-state-transition", f"{case_id} repair execution proof")
    if case_id == "SR-04" and state in {"planned", "locked-not-run"} and proof != "not-run":
        _m2_fail("m2-state-transition", "SR-04 pre-live repair proof")
    if case_id == "SR-04" and state == "completed" and proof != "not-run" and not M2_SHA256.fullmatch(proof):
        _m2_fail("m2-enums", "SR-04 repair_execution_proof_digest")
    if state in {"planned", "locked-not-run"}:
        if fields["processed_tokens"] != "not-run" or fields["wall_time_seconds"] != "not-run" or fields["privacy_retention"] != "not-applicable":
            _m2_fail("m2-state-transition", f"{case_id} pre-live record values")
    else:
        for name in ("processed_tokens", "wall_time_seconds"):
            if fields[name] != "unknown" and not _m2_is_integer(fields[name]):
                _m2_fail("m2-metering", f"{case_id} {name}")
        retention = fields["privacy_retention"]
        if retention != "deleted" and not (retention.startswith("retained-until:") and _m2_is_date(retention.removeprefix("retained-until:"))):
            _m2_fail("m2-enums", f"{case_id} privacy_retention")
    if fields["custody_retention"] != "retained-for:365d-after-decision" and not (state == "completed" and fields["custody_retention"].startswith("retained-until:") and _m2_is_date(fields["custody_retention"].removeprefix("retained-until:"))):
        _m2_fail("m2-enums", f"{case_id} custody_retention")
    return state


def _m2_attempt_totals(records: dict[str, tuple[dict[str, str], dict[str, list[str]]]]) -> tuple[int, int]:
    attempts = 0
    transport = 0
    for case_id in M2_CASE_IDS:
        _fields, arms = records[case_id]
        for arm in M2_ARMS[case_id]:
            value = arms[arm[0]][11]
            if value == "not-run":
                continue
            counters = _m2_counter(value, ("primary", "secondary", "repair", "transport-failed"), "m2-attempt-budget")
            attempts += sum(counters.values())
            transport += counters["transport-failed"]
    return attempts, transport


def _m2_is_terminal_arm(row: list[str]) -> bool:
    if row[7] in {"product-failed", "component-failed", "infrastructure-failed", "invalid-case", "budget-exhausted"}:
        return True
    return row[8] == "failed" or (row[9] != "not-applicable" and row[9] != "not-run" and int(row[9]) > 0)


def _m2_validate_terminal_sample(records: dict[str, tuple[dict[str, str], dict[str, list[str]]]], decision_state: str) -> None:
    attempts, transport = _m2_attempt_totals(records)
    if attempts > 8 or transport > 1:
        _m2_fail("m2-attempt-budget", "sample ceiling")
    if decision_state == "completed" and (attempts not in {7, 8} or (attempts == 7 and transport != 0) or (attempts == 8 and transport != 1)):
        _m2_fail("m2-attempt-budget", "completed sample total")
    if decision_state != "aborted":
        return
    stopped = False
    for case_id in M2_CASE_IDS:
        fields, arms = records[case_id]
        if fields["run_state"] != "completed":
            continue
        for arm in M2_ARMS[case_id]:
            row = arms[arm[0]]
            if row[6:] == ["not-run"] * 9:
                if not stopped:
                    _m2_fail("m2-state-transition", "unrun arm before terminal stop")
                continue
            if stopped:
                _m2_fail("m2-state-transition", "arm after terminal stop")
            stopped = _m2_is_terminal_arm(row)
    if not stopped:
        _m2_fail("m2-state-transition", "aborted sample has no terminal stop")


def _m2_validate_terminal_retention(records: dict[str, tuple[dict[str, str], dict[str, list[str]]]], decision_date: str) -> None:
    minimum = date.fromisoformat(decision_date) + timedelta(days=365)
    for case_id in M2_CASE_IDS:
        fields, _arms = records[case_id]
        if fields["run_state"] != "completed":
            continue
        retention = fields["custody_retention"]
        if not retention.startswith("retained-until:"):
            _m2_fail("m2-state-transition", f"{case_id} terminal custody retention")
        retained_until = date.fromisoformat(retention.removeprefix("retained-until:"))
        if retained_until < minimum:
            _m2_fail("m2-state-transition", f"{case_id} custody retention date")


def _m2_validate_meters(records: dict[str, tuple[dict[str, str], dict[str, list[str]]]]) -> None:
    for case_id in M2_CASE_IDS:
        fields, arms = records[case_id]
        if fields["run_state"] != "completed":
            continue
        if fields["processed_tokens"] == "unknown" or fields["wall_time_seconds"] == "unknown":
            if not any(row[7] == "infrastructure-failed" for row in arms.values()):
                _m2_fail("m2-metering", f"{case_id} unknown meter without infrastructure failure")


def _m2_validate_clear(records: dict[str, tuple[dict[str, str], dict[str, list[str]]]], evidence: str) -> None:
    for case_id in M2_CASE_IDS:
        fields, arms = records[case_id]
        if fields["processed_tokens"] == "unknown" or fields["wall_time_seconds"] == "unknown":
            _m2_fail("m2-metering", f"{case_id} clear meters")
        current = arms[M2_CASE_REGISTRY[case_id][0]]
        if current[7] != "passed" or current[8] != "passed" or current[9] != "0":
            _m2_fail("m2-overall-outcome", f"{case_id} clear current arm")
        if any(not M2_SHA256.fullmatch(row[14]) for row in arms.values()):
            _m2_fail("m2-overall-outcome", f"{case_id} clear arm receipt")
    sr03_reference = records["SR-03"][1]["SR-03.primary-only"]
    sr04_reference = records["SR-04"][1]["SR-04.fail-closed"]
    if not M2_SHA256.fullmatch(records["SR-04"][0]["repair_execution_proof_digest"]):
        _m2_fail("m2-overall-outcome", "SR-04 clear repair proof")
    if sr03_reference[6] != "review-clear" or sr03_reference[7] != "passed" or sr03_reference[13] != "dangerous-miss":
        _m2_fail("m2-overall-outcome", "SR-03 contrast reference")
    if sr04_reference[6] != "not-applicable" or sr04_reference[7] != "invalid-case":
        _m2_fail("m2-overall-outcome", "SR-04 contrast reference")
    useful = {arm_id for _case, (_fields, arms) in records.items() for arm_id, row in arms.items() if row[13] == "useful"}
    tokens = evidence.split(";")
    if not tokens or any(not re.fullmatch(r"SR-0[1-4]\.[a-z-]+:useful", token) or token.removesuffix(":useful") not in useful for token in tokens):
        _m2_fail("m2-decision-evidence", "clear evidence token")


def _m2_decisions(cases: dict[str, dict[str, object]]) -> None:
    lines = _m2_load(M2_DECISIONS_DOCUMENT)
    index = 0
    if not lines or lines[index] != "# M2 release records":
        _m2_fail("m2-document-order", "decision title")
    index += 1
    records: dict[str, tuple[dict[str, str], dict[str, list[str]]]] = {}
    for case_id in M2_CASE_IDS:
        if index >= len(lines) or lines[index] != f"## {case_id}":
            _m2_fail("m2-document-order", f"decision {case_id} heading")
        index += 1
        if index >= len(lines) or lines[index] != "### Record fields":
            _m2_fail("m2-document-order", f"decision {case_id} field heading")
        index += 1
        field_rows, index = _m2_table(lines, index, ("Field", "Value"), "m2-field-shape")
        fields = _m2_fields(field_rows, M2_RECORD_FIELDS, "m2-field-shape")
        state = _m2_validate_record(case_id, fields, cases[case_id]["fields"])
        if index >= len(lines) or lines[index] != "### Arm records":
            _m2_fail("m2-document-order", f"decision {case_id} arm heading")
        index += 1
        arm_rows, index = _m2_table(lines, index, M2_ARM_HEADER, "m2-field-shape")
        records[case_id] = (fields, _m2_arms(case_id, arm_rows, state))
    if index >= len(lines) or lines[index] != "## M2 decision":
        _m2_fail("m2-document-order", "M2 decision heading")
    index += 1
    if index >= len(lines) or lines[index] != "### Decision fields":
        _m2_fail("m2-document-order", "decision fields heading")
    index += 1
    rows, index = _m2_table(lines, index, ("Field", "Value"), "m2-field-shape")
    decision = _m2_fields(rows, ("decision_state", "decision_date", "m2_outcome", "decision", "evidence"), "m2-field-shape")
    if index != len(lines):
        _m2_fail("m2-document-order", "extra decision content")
    states = [records[case_id][0]["run_state"] for case_id in M2_CASE_IDS]
    locked_profiles = {
        tuple(records[case_id][0][name] for name in ("plugin_version", "agent_version", "model", "effort", "run_date"))
        for case_id in M2_CASE_IDS
        if records[case_id][0]["run_state"] != "planned"
    }
    if len(locked_profiles) > 1:
        _m2_fail("m2-state-transition", "execution profile drift")
    if decision["decision_state"] == "pending-live-sample":
        if any(state == "completed" for state in states) or tuple(decision.values()) != ("pending-live-sample", "not-run", "not-run", "not-applicable", "not-applicable"):
            _m2_fail("m2-state-transition", "pending decision")
        return
    if decision["decision_state"] not in {"aborted", "completed"} or not _m2_is_date(decision["decision_date"]):
        _m2_fail("m2-state-transition", "terminal decision")
    if decision["decision_state"] == "aborted":
        if decision["m2_outcome"] != "not-clear" or decision["decision"] != "not-applicable" or decision["evidence"] != "not-applicable":
            _m2_fail("m2-state-transition", "aborted decision")
        first_unfinished = next((index for index, state in enumerate(states) if state != "completed"), len(states))
        if first_unfinished == 0 or any(state == "completed" for state in states[first_unfinished:]):
            _m2_fail("m2-state-transition", "aborted record prefix")
        _m2_validate_terminal_sample(records, "aborted")
        _m2_validate_meters(records)
        _m2_validate_terminal_retention(records, decision["decision_date"])
        return
    if states != ["completed"] * len(M2_CASE_IDS) or decision["m2_outcome"] not in {"clear", "not-clear"}:
        _m2_fail("m2-state-transition", "completed decision")
    if decision["m2_outcome"] == "not-clear":
        if decision["decision"] != "not-applicable" or decision["evidence"] != "not-applicable":
            _m2_fail("m2-decision-evidence", "not-clear decision")
        _m2_validate_terminal_sample(records, "completed")
        _m2_validate_meters(records)
        _m2_validate_terminal_retention(records, decision["decision_date"])
        return
    if decision["decision"] not in {"keep", "improve", "delete"} or not decision["evidence"]:
        _m2_fail("m2-decision-evidence", "clear decision")
    _m2_validate_terminal_sample(records, "completed")
    _m2_validate_meters(records)
    _m2_validate_terminal_retention(records, decision["decision_date"])
    _m2_validate_clear(records, decision["evidence"])


def validate_m2_evaluation_documents() -> None:
    cases = _m2_cases()
    _m2_runbook()
    _m2_decisions(cases)


def validate_public_surface() -> None:
    for relative in repository_files():
        parts = set(relative.parts)
        if parts & FORBIDDEN_PATH_PARTS or relative.suffix in FORBIDDEN_SUFFIXES:
            fail(f"Запрещённый artifact: {relative}")
        path = ROOT / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in FORBIDDEN_TEXT:
            if token in text:
                fail(f"В {relative} найден внутренний marker: {token}")


def main() -> int:
    checks = (
        validate_required_files,
        validate_marketplace,
        validate_plugin_manifest,
        validate_plugin_hooks,
        validate_json_files,
        validate_model_output_schemas,
        validate_skills,
        validate_platform_profiles,
        validate_evaluation_documents,
        validate_m2_evaluation_documents,
        validate_public_surface,
    )
    try:
        for check in checks:
            check()
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("Публичный пакет Easy Peasy DLS валиден.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
