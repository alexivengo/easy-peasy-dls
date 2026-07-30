#!/usr/bin/env python3
"""Validate the public Easy Peasy DLS repository without external dependencies."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "dls"
MARKETPLACE = ROOT / ".agents" / "plugins" / "marketplace.json"
MANIFEST = PLUGIN / ".codex-plugin" / "plugin.json"
MODEL_OUTPUT_SCHEMAS = (
    PLUGIN / "assets" / "schemas" / "review-decision.schema.json",
    PLUGIN / "assets" / "schemas" / "specialist-decision.schema.json",
)

REQUIRED_FILES = (
    ROOT / "README.md",
    ROOT / "LICENSE",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    ROOT / "docs" / "capability-catalog.md",
    ROOT / "docs" / "roadmap.md",
    MARKETPLACE,
    MANIFEST,
    PLUGIN / "skills" / "dls-workflow" / "SKILL.md",
    PLUGIN / "skills" / "dls-debug" / "SKILL.md",
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
CAPABILITY_ROW = re.compile(r"^\| ([MPAIR])(\d{2}) \|", re.MULTILINE)
CAPABILITY_RANGES = {
    "M": 66,
    "P": 43,
    "A": 10,
    "I": 12,
    "R": 24,
}


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


def validate_capability_catalog() -> None:
    catalog = ROOT / "docs" / "capability-catalog.md"
    text = catalog.read_text(encoding="utf-8")
    ids = [f"{prefix}{number}" for prefix, number in CAPABILITY_ROW.findall(text)]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        fail(
            "Capability catalog содержит повторяющиеся ID: "
            + ", ".join(duplicates)
        )

    expected = {
        f"{prefix}{number:02d}"
        for prefix, maximum in CAPABILITY_RANGES.items()
        for number in range(1, maximum + 1)
    }
    actual = set(ids)
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing or unexpected:
        details = []
        if missing:
            details.append("отсутствуют " + ", ".join(missing))
        if unexpected:
            details.append("неожиданные " + ", ".join(unexpected))
        fail("Capability catalog неполон: " + "; ".join(details))


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
        validate_json_files,
        validate_model_output_schemas,
        validate_skills,
        validate_platform_profiles,
        validate_capability_catalog,
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
