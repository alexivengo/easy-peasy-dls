#!/usr/bin/env python3
"""Validate the public Easy Peasy DLS repository without external dependencies."""

from __future__ import annotations

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
