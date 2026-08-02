"""Regression coverage for public-validator local DLS state handling."""

from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path


VALIDATOR_PATH = Path(__file__).with_name("validate_public_repo.py")
SPEC = importlib.util.spec_from_file_location("validate_public_repo", VALIDATOR_PATH)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


class PublicValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous_root = validator.ROOT
        validator.ROOT = self.root

    def tearDown(self) -> None:
        validator.ROOT = self.previous_root
        self.temporary.cleanup()

    def test_untracked_local_dls_json_is_ignored(self) -> None:
        path = self.root / ".dls" / "state" / "PV-00.json"
        path.parent.mkdir(parents=True)
        path.write_text("{", encoding="utf-8")

        validator.validate_json_files()

    def test_local_dls_profile_is_ignored(self) -> None:
        path = self.root / ".dls" / "profiles" / "generic.toml"
        path.parent.mkdir(parents=True)
        path.write_text("not valid toml = [", encoding="utf-8")

        validator.validate_platform_profiles()

    def test_tracked_dls_json_remains_forbidden(self) -> None:
        path = self.root / ".dls" / "state" / "PV-00.json"
        path.parent.mkdir(parents=True)
        path.write_text("{", encoding="utf-8")
        for args in (("init",), ("add", ".dls")):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

        validator.validate_json_files()
        with self.assertRaisesRegex(ValueError, "Запрещённый artifact: .dls/state/PV-00.json"):
            validator.validate_public_surface()


if __name__ == "__main__":
    unittest.main()
