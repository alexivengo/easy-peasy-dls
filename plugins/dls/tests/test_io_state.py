from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dls_core.errors import IntegrityError, LockError
from dls_core.io import (
    GENERATED_END,
    GENERATED_START,
    FileLock,
    atomic_write_json,
    atomic_write_text,
    canonical_text,
    safe_resolve,
)
from dls_core.state import StateStore

from support import create_change, initialize


class IOAndStateTests(unittest.TestCase):
    def test_generated_regions_and_formatting_do_not_change_canonical_text(self) -> None:
        first = f"# A  \r\n{GENERATED_START}\r\nold\r\n{GENERATED_END}\r\nBody  \r\n"
        second = f"# A\n{GENERATED_START}\nnew\n{GENERATED_END}\nBody\n\n"
        self.assertEqual(canonical_text(first), canonical_text(second))

    def test_unclosed_generated_region_is_rejected(self) -> None:
        with self.assertRaises(IntegrityError):
            canonical_text(f"# A\n{GENERATED_START}\n")

    def test_atomic_json_keeps_previous_valid_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_write_json(path, {"revision": 1}, backup=False)
            atomic_write_json(path, {"revision": 2})
            self.assertEqual(json.loads(path.read_text())["revision"], 2)
            self.assertEqual(json.loads(path.with_suffix(".json.bak").read_text())["revision"], 1)

    def test_failed_atomic_replace_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "value.txt"
            path.write_text("previous", encoding="utf-8")
            with mock.patch("dls_core.io.os.replace", side_effect=OSError("fault")):
                with self.assertRaises(OSError):
                    atomic_write_text(path, "next", backup=False)
            self.assertEqual(path.read_text(encoding="utf-8"), "previous")
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_lock_refuses_concurrent_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "state.lock"
            with FileLock(lock_path):
                with self.assertRaises(LockError):
                    with FileLock(lock_path):
                        pass

    def test_path_traversal_and_symlink_escape_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "link").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(IntegrityError):
                safe_resolve(root, "../outside")
            with self.assertRaises(IntegrityError):
                safe_resolve(root, "link/value")

    def test_state_cas_backup_and_operation_kind_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root)
            create_change(root)
            store = StateStore(root)

            def mutate(value: dict) -> None:
                value["lifecycle"] = "working"

            updated, changed = store.mutate(
                "C001",
                expected_revision=1,
                operation_id="op-1",
                operation_kind="test",
                mutator=mutate,
            )
            self.assertTrue(changed)
            self.assertEqual(updated["state_revision"], 2)
            backup = json.loads(store.path("C001").with_suffix(".json.bak").read_text())
            self.assertEqual(backup["state_revision"], 1)
            replayed, changed = store.mutate(
                "C001",
                expected_revision=1,
                operation_id="op-1",
                operation_kind="test",
                mutator=mutate,
            )
            self.assertFalse(changed)
            self.assertEqual(replayed["state_revision"], 2)
            with self.assertRaises(IntegrityError):
                store.mutate(
                    "C001",
                    expected_revision=2,
                    operation_id="op-1",
                    operation_kind="different",
                    mutator=mutate,
                )
            with self.assertRaises(IntegrityError):
                store.mutate(
                    "C001",
                    expected_revision=1,
                    operation_id="op-2",
                    operation_kind="test",
                    mutator=mutate,
                )


if __name__ == "__main__":
    unittest.main()
