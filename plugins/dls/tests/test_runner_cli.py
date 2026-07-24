from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dls_core.errors import ConfigError
from dls_core.operations import validate_command

from support import create_change, git, initialize, initialize_git


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CLI = PLUGIN_ROOT / "scripts" / "dls.py"


class RunnerAndCLITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("codex"), "Codex CLI is unavailable")
    def test_plugin_install_and_remove_in_disposable_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            marketplace = sandbox / "marketplace"
            plugin_link = marketplace / "plugins" / "dls"
            plugin_link.parent.mkdir(parents=True)
            plugin_link.symlink_to(PLUGIN_ROOT, target_is_directory=True)
            manifest_dir = marketplace / ".agents" / "plugins"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "marketplace.json").write_text(
                json.dumps(
                    {
                        "name": "dls-test",
                        "interface": {"displayName": "DLS Test"},
                        "plugins": [
                            {
                                "name": "dls",
                                "source": {
                                    "source": "local",
                                    "path": "./plugins/dls",
                                },
                                "policy": {
                                    "installation": "AVAILABLE",
                                    "authentication": "ON_INSTALL",
                                },
                                "category": "Productivity",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            isolated_home = sandbox / "home"
            isolated_codex_home = sandbox / "codex-home"
            isolated_home.mkdir()
            isolated_codex_home.mkdir()
            environment["HOME"] = str(isolated_home)
            environment["CODEX_HOME"] = str(isolated_codex_home)
            added_marketplace = self._codex(
                environment,
                "plugin",
                "marketplace",
                "add",
                str(marketplace),
                "--json",
            )
            self.assertIn("dls-test", json.dumps(added_marketplace))
            installed = self._codex(
                environment,
                "plugin",
                "add",
                "dls@dls-test",
                "--json",
            )
            self.assertIn("dls", json.dumps(installed))
            removed = self._codex(
                environment,
                "plugin",
                "remove",
                "dls@dls-test",
                "--json",
            )
            self.assertIn("dls", json.dumps(removed))

    def test_cli_json_smoke_and_stale_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialized = self._cli(root, "init")
            self.assertTrue(initialized["ok"])
            created = self._cli(
                root,
                "new",
                "C001",
                "--slug",
                "cli-change",
                "--title",
                "CLI change",
                "--kind",
                "chore",
                "--control",
                "routine",
                "--outcome",
                "Exercise the CLI.",
            )
            self.assertEqual(created["state_revision"], 1)
            failed = self._cli(
                root,
                "ticket",
                "set",
                "C001",
                "T01",
                "in-progress",
                "--expect-revision",
                "99",
                expected_exit=3,
            )
            self.assertEqual(failed["error"], "IntegrityError")

    def test_cli_adopts_existing_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._cli(root, "init")
            spec = root / "docs/existing/SPEC.md"
            spec.parent.mkdir(parents=True)
            spec.write_text("# Existing contract\n", encoding="utf-8")
            preview = self._cli(
                root,
                "adopt",
                "E01",
                "--slug",
                "existing",
                "--kind",
                "feature",
                "--control",
                "standard",
                "--artifact",
                "spec=docs/existing/SPEC.md",
                "--operation-id",
                "adopt-e01",
                "--dry-run",
            )
            self.assertTrue(preview["dry_run"])
            self.assertFalse((root / ".dls/state/E01.json").exists())
            adopted = self._cli(
                root,
                "adopt",
                "E01",
                "--slug",
                "existing",
                "--kind",
                "feature",
                "--control",
                "standard",
                "--artifact",
                "spec=docs/existing/SPEC.md",
                "--operation-id",
                "adopt-e01",
            )
            self.assertTrue(adopted["changed"])
            self.assertEqual(adopted["artifacts"], ["docs/existing/SPEC.md"])

    def test_cli_manages_shared_git_worktree_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            base = sandbox / "base"
            owner = sandbox / "owner"
            base.mkdir()
            initialize_git(base)
            git(base, "worktree", "add", "-b", "codex/C001", str(owner))
            initialize(owner)
            create_change(owner)
            registered = self._cli(
                base,
                "worktree",
                "register",
                "C001",
                str(owner.resolve()),
            )
            self.assertTrue(registered["changed"])
            listed = self._cli(base, "worktree", "list")
            self.assertEqual(
                [(item["change_id"], item["valid"]) for item in listed["worktrees"]],
                [("C001", True)],
            )
            verified = self._cli(base, "worktree", "verify", "C001")
            self.assertEqual(verified["worktree"]["owner_root"], str(owner.resolve()))
            removed = self._cli(base, "worktree", "unregister", "C001")
            self.assertTrue(removed["changed"])

    def test_runner_uses_named_argv_redacts_and_caps_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            create_change(root)
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text()
                + f"""

[commands.safe]
argv = ["{sys.executable}", "-c", "print('token=secret-value')"]
cwd = "."
timeout_seconds = 5
max_output_bytes = 1024
env_allow = []

[commands.overflow]
argv = ["{sys.executable}", "-c", "print('x' * 4096)"]
cwd = "."
timeout_seconds = 5
max_output_bytes = 1024
env_allow = []

[commands.envcheck]
argv = ["{sys.executable}", "-c", "import os; print(os.environ.get('UNSAFE_SECRET'))"]
cwd = "."
timeout_seconds = 5
max_output_bytes = 1024
env_allow = []
""",
                encoding="utf-8",
            )
            safe = validate_command(
                root,
                change_id="C001",
                command_id="safe",
                expected_revision=1,
                operation_id="validate-safe",
            )
            self.assertTrue(safe["ok"])
            self.assertNotIn("secret-value", json.dumps(safe))
            overflow = validate_command(
                root,
                change_id="C001",
                command_id="overflow",
                expected_revision=2,
                operation_id="validate-overflow",
            )
            self.assertFalse(overflow["ok"])
            self.assertTrue(overflow["validation"]["output_overflow"])
            log_path = root / overflow["validation"]["redacted_log_path"]
            self.assertLessEqual(log_path.stat().st_size, 1024)
            with mock.patch.dict(os.environ, {"UNSAFE_SECRET": "must-not-leak"}):
                environment_check = validate_command(
                    root,
                    change_id="C001",
                    command_id="envcheck",
                    expected_revision=3,
                    operation_id="validate-envcheck",
                )
            self.assertTrue(environment_check["ok"])
            self.assertNotIn("must-not-leak", json.dumps(environment_check))

    def test_runner_timeout_and_unknown_command_fail_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            create_change(root)
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text()
                + f"""

[commands.slow]
argv = ["{sys.executable}", "-c", "import time; time.sleep(3)"]
cwd = "."
timeout_seconds = 1
max_output_bytes = 1024
env_allow = []
""",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                validate_command(
                    root,
                    change_id="C001",
                    command_id="from-markdown",
                    expected_revision=1,
                    operation_id="unknown",
                )
            timed = validate_command(
                root,
                change_id="C001",
                command_id="slow",
                expected_revision=1,
                operation_id="validate-slow",
            )
            self.assertFalse(timed["ok"])
            self.assertTrue(timed["validation"]["timed_out"])
            self.assertEqual(timed["evidence"]["exit_code"], 124)

    def test_runner_dry_run_does_not_execute(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            create_change(root)
            marker = root / "marker.txt"
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text()
                + f"""

[commands.marker]
argv = ["{sys.executable}", "-c", "open('marker.txt', 'w').write('ran')"]
cwd = "."
timeout_seconds = 5
max_output_bytes = 1024
env_allow = []
""",
                encoding="utf-8",
            )
            result = validate_command(
                root,
                change_id="C001",
                command_id="marker",
                expected_revision=1,
                operation_id="validate-marker",
                dry_run=True,
            )
            self.assertTrue(result["dry_run"])
            self.assertFalse(marker.exists())

    def _cli(
        self,
        root: Path,
        *arguments: str,
        expected_exit: int = 0,
    ) -> dict:
        result = subprocess.run(
            [
                sys.executable,
                str(CLI),
                "--root",
                str(root),
                "--json",
                *arguments,
            ],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(result.returncode, expected_exit, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def _codex(self, environment: dict[str, str], *arguments: str) -> dict:
        result = subprocess.run(
            [shutil.which("codex") or "codex", *arguments],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)


if __name__ == "__main__":
    unittest.main()
