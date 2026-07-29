from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from dls_core.candidate_runner import candidate_ready, candidate_status
from dls_core.errors import ConfigError, IntegrityError
from dls_core.operations import _review_context_v2, build_context, doctor
from dls_core.repo import PROFILE_CONTRACT, load_config, resolve_profile
from dls_core.state import StateStore
from dls_core.telemetry import review_metrics

from support import create_change, git, initialize, initialize_git
from dls_core.operations import approve


class PlatformProfilesV070Tests(unittest.TestCase):
    def _profile_root(self, directory: str) -> Path:
        root = Path(directory)
        initialize_git(root)
        initialize(root)
        return root

    def _candidate_root(self, directory: str) -> tuple[Path, str]:
        root = self._profile_root(directory)
        base = git(root, "rev-parse", "HEAD")
        create_change(root, control="standard")
        config = root / ".dls/config.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                "[policy]\n",
                '[policy]\nreview_required_commands = ["canonical"]\n',
                1,
            )
            + f"""

[commands.canonical]
argv = ["{sys.executable}", "-c", "print('canonical pass')"]
cwd = "."
timeout_seconds = 5
max_output_bytes = 4096
env_allow = []
""",
            encoding="utf-8",
        )
        git(root, "add", ".dls", "docs")
        git(root, "commit", "-m", "candidate definition")
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
        return root, base

    def _write_profile(self, root: Path, name: str, body: str) -> None:
        profile = root / ".dls/profiles" / f"{name}.toml"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(body, encoding="utf-8")

    def test_bundled_backend_profile_resolves_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._profile_root(directory)
            config = load_config(root)
            config["default_profile"] = "server-backend"
            first = resolve_profile(root, config=config)
            second = resolve_profile(root, config=config)
            self.assertEqual(first, second)
            self.assertEqual(first["contract"], PROFILE_CONTRACT)
            self.assertEqual(first["inheritance_chain"], ["generic", "server-backend"])
            self.assertIn("integration-test", first["common_evidence_types"])
            self.assertIn("rollback-drill", first["platform_evidence_types"])
            self.assertIn("backend-architecture", first["domain_capabilities"])
            self.assertTrue(first["domain_skills_are_advisory"])
            self.assertEqual(first["process_owner"], "dls")
            encoded = json.dumps(first).lower()
            self.assertNotIn("app-store", encoded)
            self.assertNotIn("swiftui", encoded)

    def test_repository_profile_shadows_bundled_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._profile_root(directory)
            self._write_profile(
                root,
                "generic",
                """schema_version = 1
name = "generic"

[discovery]
hints = ["Repository-owned discovery hint."]

[routing]
domain_skills_are_advisory = true
process_owner = "dls"
""",
            )
            resolved = resolve_profile(root, config=load_config(root))
            self.assertEqual(resolved["source"], "repository")
            self.assertEqual(
                resolved["discovery_hints"], ["Repository-owned discovery hint."]
            )

    def test_profile_integrity_rejects_unsafe_documents(self) -> None:
        fixtures = {
            "unknown": """schema_version = 1
name = "unknown"
[commands]
argv = ["bash"]
""",
            "mismatch": """schema_version = 1
name = "different"
""",
            "unsafe": """schema_version = 1
name = "unsafe"
[routing]
domain_skills = ["../../escape"]
domain_skills_are_advisory = true
process_owner = "dls"
""",
            "owner": """schema_version = 1
name = "owner"
[routing]
domain_skills_are_advisory = true
process_owner = "agent"
""",
        }
        for name, body in fixtures.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = self._profile_root(directory)
                self._write_profile(root, name, body)
                with self.assertRaises(ConfigError):
                    resolve_profile(root, config={"default_profile": name})

    def test_profile_cycles_depth_size_and_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._profile_root(directory)
            self._write_profile(root, "cycle-a", 'schema_version = 1\nname = "cycle-a"\nextends = "cycle-b"\n')
            self._write_profile(root, "cycle-b", 'schema_version = 1\nname = "cycle-b"\nextends = "cycle-a"\n')
            with self.assertRaises(ConfigError):
                resolve_profile(root, config={"default_profile": "cycle-a"})

            for index in range(9):
                parent = f'\nextends = "depth-{index + 1}"' if index < 8 else ""
                self._write_profile(
                    root,
                    f"depth-{index}",
                    f'schema_version = 1\nname = "depth-{index}"{parent}\n',
                )
            with self.assertRaises(ConfigError):
                resolve_profile(root, config={"default_profile": "depth-0"})

            oversized = root / ".dls/profiles/oversized.toml"
            oversized.write_text(
                'schema_version = 1\nname = "oversized"\n#' + ("x" * 70000),
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                resolve_profile(root, config={"default_profile": "oversized"})

            link = root / ".dls/profiles/linked.toml"
            link.symlink_to(root / ".dls/profiles/oversized.toml")
            with self.assertRaises(ConfigError):
                resolve_profile(root, config={"default_profile": "linked"})

    def test_context_doctor_pack_and_metrics_expose_bounded_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, base = self._candidate_root(directory)
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'default_profile = "generic"',
                    'default_profile = "server-backend"',
                ),
                encoding="utf-8",
            )
            context = build_context(
                root,
                change_id="C001",
                phase="implementation",
                include=[],
                exclude=[],
                dry_run=True,
            )["manifest"]
            profile = context["platform_profile"]
            self.assertEqual(profile["name"], "server-backend")
            self.assertIn("discovery_hints", profile)
            self.assertNotIn(str(root), json.dumps(profile))

            diagnostic = doctor(root)
            self.assertEqual(diagnostic["platform_profile"]["digest"], profile["digest"])
            self.assertEqual(diagnostic["platform_profile"]["source"], "bundled")

            ready = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            pack = json.loads((root / ready["review_pack_path"]).read_text())
            self.assertEqual(
                pack["platform_profile"],
                {
                    "contract": PROFILE_CONTRACT,
                    "name": "server-backend",
                    "digest": profile["digest"],
                },
            )
            metrics = review_metrics(root, change_id="C001")
            self.assertEqual(metrics["platform_profile"], pack["platform_profile"])
            review_context = _review_context_v2(
                root,
                change_id="C001",
                pack=pack,
            )
            self.assertEqual(
                review_context["manifest"]["platform_profile"]["name"],
                "server-backend",
            )
            projection_input = next(
                item
                for item in review_context["manifest"]["inputs"]
                if item.get("reason") == "active-review-pack"
            )
            projection = json.loads((root / projection_input["path"]).read_text())
            self.assertEqual(projection["platform_profile"]["name"], "server-backend")

    def test_profile_drift_creates_new_run_revalidates_and_stales_pack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, base = self._candidate_root(directory)
            first = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            state = StateStore(root).load("C001")
            self.assertEqual(len(state["evidence"]), 1)

            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8").replace(
                    'default_profile = "generic"',
                    'default_profile = "server-backend"',
                ),
                encoding="utf-8",
            )
            stale = candidate_status(root, change_id="C001")
            self.assertFalse(stale["prepared"])
            self.assertEqual(stale["next_action"]["id"], "run-candidate-ready")

            second = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            self.assertNotEqual(first["run_id"], second["run_id"])
            self.assertNotEqual(first["review_pack_path"], second["review_pack_path"])
            state = StateStore(root).load("C001")
            self.assertEqual(len(state["evidence"]), 2)
            self.assertEqual(
                state["candidate_runs"][-1]["platform_profile_name"],
                "server-backend",
            )

    def test_legacy_pack_without_profile_marker_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, base = self._candidate_root(directory)
            ready = candidate_ready(
                root,
                change_id="C001",
                base_ref=base,
                addressed=[],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            pack_path = root / ready["review_pack_path"]
            pack = json.loads(pack_path.read_text())
            pack.pop("platform_profile")
            pack.pop("pack_digest")
            from dls_core.operations import _review_pack_digest, _validate_review_pack

            pack["pack_digest"] = _review_pack_digest(pack)
            _validate_review_pack(pack, "C001")

    def test_workflow_skill_routes_backend_advisory_without_apple_only_gates(self) -> None:
        skill = (
            Path(__file__).resolve().parents[1]
            / "skills/dls-workflow/SKILL.md"
        ).read_text(encoding="utf-8")
        self.assertIn("doctor.platform_profile", skill)
        self.assertIn("server-backend", skill)
        self.assertIn("Missing domain skills never block delivery", skill)
        self.assertIn("Swift architecture, concurrency, and testing", skill)
        self.assertIn("Do not route it through Apple UI, App Store", skill)
        self.assertNotIn("implicit profile detection", skill.lower())


if __name__ == "__main__":
    unittest.main()
