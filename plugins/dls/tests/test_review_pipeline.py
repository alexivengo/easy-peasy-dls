from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dls_core.economy import ReviewBudget
from dls_core.errors import IntegrityError, UsageError
from dls_core.operations import (
    approve,
    build_context,
    check,
    evidence_add,
    review_import,
    review_pack,
    review_start,
    ticket_set,
)
from dls_core.state import StateStore
from dls_core.worktrees import (
    worktree_list,
    worktree_register,
    worktree_unregister,
    worktree_verify,
)

from support import (
    build_review_report,
    create_change,
    git,
    initialize,
    initialize_git,
    start_review_with_fake_codex,
)


class ReviewPipelineTests(unittest.TestCase):
    def _standard_pack(
        self,
        root: Path,
        *,
        tickets: bool = False,
    ) -> tuple[str, dict]:
        base_sha = initialize_git(root)
        initialize(root)
        create_change(root, control="standard", tickets=tickets)
        git(root, "add", ".dls", "docs")
        git(root, "commit", "-m", "approved definition")
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
            operation_id="definition-1",
        )
        revision = 2
        if tickets:
            ticket_set(
                root,
                change_id="C001",
                ticket_id="T01",
                ticket_status="in-progress",
                expected_revision=revision,
                note=None,
                operation_id="ticket-start",
            )
            revision += 1
            ticket_set(
                root,
                change_id="C001",
                ticket_id="T01",
                ticket_status="implemented",
                expected_revision=revision,
                note=None,
                operation_id="ticket-implemented",
            )
            revision += 1
        head_sha = git(root, "rev-parse", "HEAD")
        evidence_add(
            root,
            change_id="C001",
            command_id="test",
            exit_code=0,
            summary="PASS",
            expected_revision=revision,
            git_sha=head_sha,
            artifacts=[],
            environment="fixture",
            duration_seconds=0.1,
            operation_id="evidence-1",
        )
        revision += 1
        pack = review_pack(
            root,
            change_id="C001",
            base_ref=base_sha,
            head_ref=None,
            expected_revision=revision,
            advisory_dirty=False,
            operation_id="review-pack-1",
        )
        return base_sha, pack

    def _install_fake_codex(self, root: Path, script: str) -> str | None:
        fake_bin = root / ".dls" / "cache" / "fake-bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        executable = fake_bin / "codex"
        executable.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" != \"exec\" ]; then exit 64; fi\n"
            "shift\n"
            "final_output_path=''\n"
            "review_seen='false'\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"review\" ]; then\n"
            "    review_seen='true'\n"
            "    shift\n"
            "  elif [ \"$1\" = \"-o\" ] || [ \"$1\" = \"--output-last-message\" ]; then\n"
            "    final_output_path=\"$2\"\n"
            "    shift 2\n"
            "  else\n"
            "    shift\n"
            "  fi\n"
            "done\n"
            "if [ \"$review_seen\" != \"true\" ] || [ -z \"$final_output_path\" ]; then exit 65; fi\n"
            "printf '{\"summary\":\"No findings.\",\"findings\":[]}\\n' "
            "> \"$final_output_path\"\n"
            + script,
            encoding="utf-8",
        )
        executable.chmod(0o755)
        original = os.environ.get("PATH")
        os.environ["PATH"] = f"{fake_bin}{os.pathsep}{original or ''}"
        return original

    def _restore_path(self, original: str | None) -> None:
        if original is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original

    def _standard_pack_in_linked_worktree(
        self,
        base: Path,
        owner: Path,
    ) -> tuple[str, dict]:
        base_sha = initialize_git(base)
        git(base, "worktree", "add", "-b", "codex/C001", str(owner))
        initialize(owner)
        create_change(owner, control="standard")
        git(owner, "add", ".dls", "docs")
        git(owner, "commit", "-m", "linked worktree definition")
        approve(
            owner,
            change_id="C001",
            decision="definition",
            expected_revision=1,
            actor="user",
            prompt=None,
            response=None,
            git_sha=None,
            conditions=None,
            operation_id="definition-linked",
        )
        head_sha = git(owner, "rev-parse", "HEAD")
        evidence_add(
            owner,
            change_id="C001",
            command_id="test",
            exit_code=0,
            summary="PASS",
            expected_revision=2,
            git_sha=head_sha,
            artifacts=[],
            environment="fixture",
            duration_seconds=0.1,
            operation_id="evidence-linked",
        )
        pack = review_pack(
            owner,
            change_id="C001",
            base_ref=base_sha,
            head_ref=None,
            expected_revision=3,
            advisory_dirty=False,
            operation_id="review-pack-linked",
        )
        return base_sha, pack

    def test_registered_worktree_routes_review_without_initializing_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            base = sandbox / "base"
            owner = sandbox / "owner"
            base.mkdir()
            _, pack = self._standard_pack_in_linked_worktree(base, owner)
            preview = worktree_register(
                base,
                change_id="C001",
                owner_path=owner.resolve(),
                dry_run=True,
            )
            self.assertTrue(preview["dry_run"])
            self.assertFalse(Path(preview["registry_path"]).exists())
            registered = worktree_register(
                base,
                change_id="C001",
                owner_path=owner.resolve(),
            )
            self.assertTrue(registered["changed"])
            self.assertIn(
                str(Path(".git") / "dls" / "worktrees.json"),
                registered["registry_path"],
            )
            self.assertTrue(worktree_verify(base, change_id="C001")["ok"])
            self.assertEqual(len(worktree_list(base)["worktrees"]), 1)
            original = self._install_fake_codex(owner, "printf 'clean\\n'\n")
            try:
                result = review_start(
                    base,
                    change_id="C001",
                    pack_path=None,
                    operation_id="registered-route",
                )
            finally:
                self._restore_path(original)
            self.assertEqual(result["owner_selection"], "registered-worktree")
            self.assertEqual(Path(result["owner_root"]), owner.resolve())
            self.assertEqual(result["review_id"], pack["review_id"])
            self.assertFalse((base / ".dls/config.toml").exists())
            removed = worktree_unregister(base, change_id="C001")
            self.assertTrue(removed["changed"])
            with self.assertRaisesRegex(IntegrityError, "No registered worktree"):
                review_start(
                    base,
                    change_id="C001",
                    pack_path=None,
                    operation_id="route-after-unregister",
                )

    def test_worktree_registry_rejects_relative_cross_repo_and_stale_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            base = sandbox / "base"
            owner = sandbox / "owner"
            other = sandbox / "other"
            base.mkdir()
            other.mkdir()
            self._standard_pack_in_linked_worktree(base, owner)
            initialize_git(other)
            initialize(other)
            create_change(other, change_id="C001")
            with self.assertRaises(UsageError):
                worktree_register(
                    base,
                    change_id="C001",
                    owner_path=Path("owner"),
                )
            with self.assertRaisesRegex(IntegrityError, "another Git repository"):
                worktree_register(
                    base,
                    change_id="C001",
                    owner_path=other.resolve(),
                )
            worktree_register(
                base,
                change_id="C001",
                owner_path=owner.resolve(),
            )
            git(owner, "switch", "-c", "codex/C001-moved")
            with self.assertRaisesRegex(IntegrityError, "branch changed"):
                worktree_verify(base, change_id="C001")
            listed = worktree_list(base)
            self.assertFalse(listed["ok"])
            self.assertFalse(listed["worktrees"][0]["valid"])

    def test_wrong_checkout_fails_without_branch_or_worktree_inference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            owner = sandbox / "owner"
            wrong = sandbox / "wrong"
            owner.mkdir()
            wrong.mkdir()
            self._standard_pack(owner)
            initialize_git(wrong)
            branch_before = git(wrong, "branch", "--show-current")
            with self.assertRaisesRegex(IntegrityError, "will not infer"):
                review_start(
                    wrong,
                    change_id="C001",
                    pack_path=None,
                    operation_id="wrong-checkout",
                )
            self.assertEqual(git(wrong, "branch", "--show-current"), branch_before)
            self.assertFalse((wrong / ".dls/config.toml").exists())

    def test_absolute_pack_selects_owner_checkout_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            owner = sandbox / "owner"
            wrong = sandbox / "wrong"
            owner.mkdir()
            wrong.mkdir()
            _, pack = self._standard_pack(owner)
            initialize_git(wrong)
            absolute_pack = (
                owner / pack["review_pack_path"]
            ).resolve()
            original = self._install_fake_codex(wrong, "printf 'clean\\n'\n")
            try:
                result = review_start(
                    wrong,
                    change_id="C001",
                    pack_path=str(absolute_pack),
                    operation_id="absolute-pack",
                )
            finally:
                self._restore_path(original)
            self.assertEqual(Path(result["owner_root"]), owner.resolve())
            self.assertEqual(result["native"]["status"], "completed")
            self.assertFalse((wrong / ".dls/state/C001.json").exists())

    def test_tampered_pack_wrong_head_and_dirty_source_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            tampered = sandbox / "tampered"
            tampered.mkdir()
            _, tampered_pack = self._standard_pack(tampered)
            pack_path = tampered / tampered_pack["review_pack_path"]
            payload = json.loads(pack_path.read_text(encoding="utf-8"))
            payload["changed_files"].append("forged.txt")
            pack_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "digest mismatch"):
                review_start(
                    tampered,
                    change_id="C001",
                    pack_path=None,
                    operation_id="tampered",
                )

            wrong_head = sandbox / "wrong-head"
            wrong_head.mkdir()
            self._standard_pack(wrong_head)
            (wrong_head / "HEAD-CHANGE.md").write_text("next\n", encoding="utf-8")
            git(wrong_head, "add", "HEAD-CHANGE.md")
            git(wrong_head, "commit", "-m", "advance head")
            wrong_head_result = review_start(
                wrong_head,
                change_id="C001",
                pack_path=None,
                operation_id="wrong-head",
            )
            self.assertFalse(wrong_head_result["ok"])
            self.assertEqual(
                wrong_head_result["next_action"]["id"],
                "provide-review-base",
            )

            dirty = sandbox / "dirty"
            dirty.mkdir()
            self._standard_pack(dirty)
            (dirty / "README.md").write_text("# dirty\n", encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "source changed"):
                review_start(
                    dirty,
                    change_id="C001",
                    pack_path=None,
                    operation_id="dirty-source",
                )

    def test_native_timeout_final_cap_and_transcript_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            timeout_root = sandbox / "timeout"
            timeout_root.mkdir()
            self._standard_pack(timeout_root)
            original = self._install_fake_codex(timeout_root, "sleep 2\n")
            try:
                with mock.patch(
                    "dls_core.operations.review_budget",
                    return_value=ReviewBudget(
                        aggregate_tokens=3_000_000,
                        lane_tokens=1_500_000,
                        command_events=24,
                        timeout_seconds=1,
                        transcript_bytes=768 * 1024,
                    ),
                ):
                    timed_out = review_start(
                        timeout_root,
                        change_id="C001",
                        pack_path=None,
                        operation_id="native-timeout",
                    )
                    repeated = review_start(
                        timeout_root,
                        change_id="C001",
                        pack_path=None,
                        operation_id="native-timeout",
                    )
            finally:
                self._restore_path(original)
            self.assertEqual(timed_out["next_action"]["id"], "inspect-review-budget")
            self.assertEqual(repeated["next_action"]["id"], "inspect-review-budget")
            timeout_state = StateStore(timeout_root).load("C001")
            self.assertEqual(timeout_state["reviews"][-1]["status"], "budget-exceeded")
            self.assertTrue(timeout_state["reviews"][-1]["timed_out"])
            self.assertEqual(
                len(
                    [
                        item
                        for item in timeout_state["reviews"]
                        if item.get("lane_key") == "native"
                    ]
                ),
                1,
            )

            transcript_root = sandbox / "transcript"
            transcript_root.mkdir()
            self._standard_pack(transcript_root)
            original = self._install_fake_codex(
                transcript_root,
                "python3 -c 'print(\"x\" * 4096)'\n",
            )
            try:
                with mock.patch(
                    "dls_core.operations.review_budget",
                    return_value=ReviewBudget(
                        aggregate_tokens=3_000_000,
                        lane_tokens=1_500_000,
                        command_events=24,
                        timeout_seconds=900,
                        transcript_bytes=1024,
                    ),
                ):
                    transcript_result = review_start(
                        transcript_root,
                        change_id="C001",
                        pack_path=None,
                        operation_id="native-transcript",
                    )
            finally:
                self._restore_path(original)
            transcript_entry = transcript_result["native"]
            self.assertEqual(transcript_entry["status"], "budget-exceeded")
            self.assertEqual(
                transcript_result["next_action"]["id"],
                "inspect-review-budget",
            )
            self.assertTrue(transcript_entry["transcript_truncated"])
            self.assertEqual(transcript_entry["transcript_output_bytes"], 4097)
            self.assertEqual(
                (transcript_root / transcript_entry["transcript_path"]).stat().st_size,
                1024,
            )
            self.assertIsNotNone(transcript_entry["output_path"])

            final_cap_root = sandbox / "final-cap"
            final_cap_root.mkdir()
            self._standard_pack(final_cap_root)
            original = self._install_fake_codex(
                final_cap_root,
                "python3 -c 'print(\"x\" * 4096)' > \"$final_output_path\"\n",
            )
            try:
                with mock.patch(
                    "dls_core.operations.NATIVE_REVIEW_MAX_OUTPUT_BYTES",
                    1024,
                ):
                    with self.assertRaisesRegex(
                        IntegrityError,
                        "exhausted automatic attempts",
                    ):
                        review_start(
                            final_cap_root,
                            change_id="C001",
                            pack_path=None,
                            operation_id="native-final-cap",
                        )
            finally:
                self._restore_path(original)
            final_cap_state = StateStore(final_cap_root).load("C001")
            self.assertEqual(final_cap_state["reviews"][-1]["status"], "output-cap")
            self.assertEqual(final_cap_state["reviews"][-1]["output_bytes"], 4097)

            missing_final_root = sandbox / "missing-final"
            missing_final_root.mkdir()
            self._standard_pack(missing_final_root)
            original = self._install_fake_codex(
                missing_final_root,
                "rm \"$final_output_path\"\n",
            )
            try:
                with self.assertRaisesRegex(
                    IntegrityError,
                    "exhausted automatic attempts",
                ):
                    review_start(
                        missing_final_root,
                        change_id="C001",
                        pack_path=None,
                        operation_id="native-missing-final",
                    )
            finally:
                self._restore_path(original)
            missing_final_state = StateStore(missing_final_root).load("C001")
            self.assertEqual(
                missing_final_state["reviews"][-1]["status"],
                "missing-output",
            )

            changed_root = sandbox / "source-changed"
            changed_root.mkdir()
            self._standard_pack(changed_root)
            original = self._install_fake_codex(
                changed_root,
                "printf 'changed by reviewer\\n' > README.md\nprintf 'done\\n'\n",
            )
            try:
                with self.assertRaisesRegex(IntegrityError, "status=source-changed"):
                    review_start(
                        changed_root,
                        change_id="C001",
                        pack_path=None,
                        operation_id="native-source-changed",
                    )
            finally:
                self._restore_path(original)
            changed_state = StateStore(changed_root).load("C001")
            self.assertEqual(changed_state["reviews"][-1]["status"], "source-changed")
            self.assertEqual(
                len(
                    [
                        item
                        for item in changed_state["reviews"]
                        if item.get("lane_key") == "native"
                    ]
                ),
                1,
            )

    def test_successful_native_is_reused_and_missing_cache_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._standard_pack(root)
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="native-first",
            )
            replayed = review_start(
                root,
                change_id="C001",
                pack_path=None,
                operation_id="native-second",
            )
            self.assertFalse(replayed["changed"])
            self.assertTrue(replayed["native_reused"])
            self.assertEqual(
                replayed["native"]["attempt_id"],
                started["native"]["attempt_id"],
            )
            self.assertIn(
                "read-only",
                replayed["native"]["argv"],
            )
            self.assertEqual(
                replayed["native"]["argv"][:2],
                ["codex", "exec"],
            )
            self.assertIn("review", replayed["native"]["argv"])
            self.assertIn("--ignore-user-config", replayed["native"]["argv"])
            self.assertIn("--json", replayed["native"]["argv"])
            self.assertIn("--ephemeral", replayed["native"]["argv"])
            self.assertIn(
                "--output-last-message",
                replayed["native"]["argv"],
            )
            (root / started["native"]["transcript_path"]).unlink()
            with self.assertRaisesRegex(
                IntegrityError,
                "diagnostic transcript is missing",
            ):
                review_start(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="native-missing-transcript",
                )
            (root / started["native"]["transcript_path"]).write_text(
                "fake codex transcript\n",
                encoding="utf-8",
            )
            (root / started["native"]["output_path"]).unlink()
            with self.assertRaisesRegex(IntegrityError, "cache is missing"):
                review_start(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="native-missing-cache",
                )

    def test_native_review_uses_clean_standalone_clone_not_owner_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, pack = self._standard_pack(root)
            owner_only = root / ".dls" / "cache" / "owner-only"
            owner_only.write_text("must not be visible to native review\n", encoding="utf-8")
            observed_cwd = root / ".dls" / "cache" / "native-cwd"
            original = self._install_fake_codex(
                root,
                (
                    "test -d .git || exit 70\n"
                    "test ! -e .dls/cache/owner-only || exit 71\n"
                    "test -z \"$(git status --porcelain)\" || exit 72\n"
                    "test -z \"$(git remote)\" || exit 73\n"
                    "test ! -e .git/objects/info/alternates || exit 74\n"
                    f"printf '%s' \"$PWD\" > {str(observed_cwd)!r}\n"
                ),
            )
            try:
                result = review_start(
                    root,
                    change_id="C001",
                    pack_path=None,
                    operation_id="native-isolated",
                )
            finally:
                self._restore_path(original)

            pack_document = pack["review_pack"]
            self.assertEqual(
                pack_document["native_workspace_contract"],
                "dls-native-workspace/v1",
            )
            native = result["native"]
            self.assertEqual(
                native["native_workspace_contract"],
                pack_document["native_workspace_contract"],
            )
            self.assertEqual(native["workspace_isolation"], "standalone-clone")
            self.assertEqual(native["workspace_head_sha"], pack_document["head_sha"])
            self.assertEqual(
                native["workspace_source_snapshot_before"],
                native["workspace_source_snapshot_after"],
            )
            self.assertIn("--cd", native["argv"])
            self.assertIn("<dls-native-workspace>", native["argv"])
            self.assertIn("<dls-native-output>", native["argv"])
            self.assertNotIn(str(root), " ".join(native["argv"]))
            native_cwd = Path(observed_cwd.read_text(encoding="utf-8"))
            self.assertNotEqual(native_cwd, root.resolve())
            self.assertFalse(native_cwd.exists())

    def test_native_attempt_without_workspace_provenance_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._standard_pack(root)
            first = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="native-legacy",
            )
            first_attempt_id = first["native"]["attempt_id"]
            store = StateStore(root)
            state = store.load("C001")

            def strip_workspace_provenance(value: dict) -> None:
                attempt = next(
                    item
                    for item in value["reviews"]
                    if item.get("attempt_id") == first_attempt_id
                )
                for field in (
                    "native_workspace_contract",
                    "workspace_isolation",
                    "workspace_head_sha",
                    "workspace_source_snapshot_before",
                    "workspace_source_snapshot_after",
                ):
                    attempt.pop(field, None)
                attempt["argv"] = ["codex", "exec", "review", "--base", "HEAD~1"]

            store.mutate(
                "C001",
                expected_revision=state["state_revision"],
                operation_id="fixture-strip-native-provenance",
                operation_kind="fixture",
                mutator=strip_workspace_provenance,
            )
            recovered = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="native-current",
            )
            self.assertNotEqual(recovered["native"]["attempt_id"], first_attempt_id)
            attempts = [
                item
                for item in StateStore(root).load("C001")["reviews"]
                if item.get("lane_key") == "native"
            ]
            self.assertEqual(
                [item["status"] for item in attempts],
                ["incompatible-workspace", "completed"],
            )

    def test_pre_runner_native_entry_without_lane_key_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._standard_pack(root)
            first = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="native-pre-runner",
            )
            first_attempt_id = first["native"]["attempt_id"]
            store = StateStore(root)
            state = store.load("C001")

            def make_pre_runner_attempt(value: dict) -> None:
                attempt = next(
                    item
                    for item in value["reviews"]
                    if item.get("attempt_id") == first_attempt_id
                )
                attempt.pop("lane_key", None)
                attempt.pop("native_workspace_contract", None)
                attempt.pop("workspace_isolation", None)
                attempt.pop("workspace_head_sha", None)
                attempt["argv"] = ["codex", "exec", "review", "--base", "HEAD~1"]

            store.mutate(
                "C001",
                expected_revision=state["state_revision"],
                operation_id="fixture-pre-runner-native",
                operation_kind="fixture",
                mutator=make_pre_runner_attempt,
            )
            recovered = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="native-after-pre-runner",
            )
            self.assertNotEqual(recovered["native"]["attempt_id"], first_attempt_id)
            updated = StateStore(root).load("C001")
            old = next(
                item
                for item in updated["reviews"]
                if item.get("attempt_id") == first_attempt_id
            )
            self.assertNotIn("lane_key", old)
            self.assertEqual(old["status"], "completed")
            self.assertEqual(recovered["native"]["status"], "completed")

    def test_import_requires_native_metadata_and_rejects_semantic_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            missing_native = sandbox / "missing-native"
            missing_native.mkdir()
            _, pack = self._standard_pack(missing_native)
            context = build_context(
                missing_native,
                change_id="C001",
                phase="review",
                include=[],
                exclude=[],
            )
            synthetic_start = {
                "semantic_model": "gpt-5.6-sol",
                "semantic_reasoning_effort": "high",
                "review_context_path": context["manifest_path"],
                "review_context_digest": context["manifest"]["manifest_digest"],
                "native": None,
            }
            report = build_review_report(
                missing_native,
                pack_result=pack,
                start_result=synthetic_start,
                verdict="review-clear",
            )
            report_path = missing_native / ".dls/cache/no-native.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "successful native pass"):
                review_import(
                    missing_native,
                    change_id="C001",
                    report_path=".dls/cache/no-native.json",
                    expected_revision=4,
                    operation_id="import-no-native",
                )

            drifted = sandbox / "drifted"
            drifted.mkdir()
            _, drifted_pack = self._standard_pack(drifted)
            started = start_review_with_fake_codex(
                drifted,
                change_id="C001",
                operation_id="native-drift",
            )
            drifted_report = build_review_report(
                drifted,
                pack_result=drifted_pack,
                start_result=started,
                verdict="review-clear",
            )
            drifted_report_path = drifted / ".dls/cache/drifted.json"
            drifted_report_path.write_text(
                json.dumps(drifted_report),
                encoding="utf-8",
            )
            (drifted / "README.md").write_text("# semantic drift\n", encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "source changed"):
                review_import(
                    drifted,
                    change_id="C001",
                    report_path=".dls/cache/drifted.json",
                    expected_revision=StateStore(drifted).load("C001")[
                        "state_revision"
                    ],
                    operation_id="import-drifted",
                )

    def test_ticket_verdicts_and_release_only_findings_do_not_block_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, pack = self._standard_pack(root, tickets=True)
            started = start_review_with_fake_codex(
                root,
                change_id="C001",
                operation_id="native-ticketed",
            )
            base_finding = {
                "id": "R001",
                "severity": "blocker",
                "kind": "external",
                "location": "release evidence",
                "issue": "Production transcript is not yet captured.",
                "impact": "Release readiness remains open.",
                "required_fix": "Capture it before release.",
                "ticket_ids": ["T01"],
                "requirement_ids": [],
                "base_sha": pack["review_pack"]["base_sha"],
                "head_sha": pack["review_pack"]["head_sha"],
            }
            legacy_blocking = build_review_report(
                root,
                pack_result=pack,
                start_result=started,
                verdict="review-clear",
                findings=[base_finding],
                ticket_verdicts=[
                    {
                        "ticket_id": "T01",
                        "verdict": "clear",
                        "finding_ids": ["R001"],
                    }
                ],
            )
            legacy_path = root / ".dls/cache/legacy-blocking.json"
            legacy_path.write_text(json.dumps(legacy_blocking), encoding="utf-8")
            with self.assertRaisesRegex(IntegrityError, "cannot be clear"):
                review_import(
                    root,
                    change_id="C001",
                    report_path=".dls/cache/legacy-blocking.json",
                    expected_revision=StateStore(root).load("C001")[
                        "state_revision"
                    ],
                    operation_id="legacy-blocking",
                )

            release_finding = dict(base_finding)
            release_finding["blocks"] = ["release", "production"]
            release_report = json.loads(json.dumps(legacy_blocking))
            release_report["findings"] = [release_finding]
            release_path = root / ".dls/cache/release-only.json"
            release_path.write_text(json.dumps(release_report), encoding="utf-8")
            imported = review_import(
                root,
                change_id="C001",
                report_path=".dls/cache/release-only.json",
                expected_revision=StateStore(root).load("C001")["state_revision"],
                operation_id="release-only",
            )
            self.assertTrue(imported["ok"])
            self.assertIsNone(imported["remediation_manifest_path"])
            accept = check(root, change_id="C001", gate="accept")
            finding_checks = {
                item["id"]: item["ok"]
                for item in accept["checks"]
                if item["id"].startswith("findings:")
            }
            self.assertEqual(
                finding_checks,
                {
                    "findings:no-open-blockers": True,
                    "findings:no-unaccepted-should-fix": True,
                },
            )


if __name__ == "__main__":
    unittest.main()
