from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from dls_core.candidate_runner import candidate_ready, candidate_status
from dls_core.errors import IntegrityError
from dls_core.review_runner import review_run, review_status
from dls_core.state import StateStore
from dls_core.telemetry import (
    METRICS_CONTRACT,
    TASK_CONTEXT_CONTRACT,
    bind_task_context,
    candidate_task_context,
    delivery_status,
    review_metrics,
    review_task_context,
    task_cycle_ref,
)

from support import git
import test_candidate_runner_v040 as candidate_tests
import test_review_runner_v030 as review_tests


class ContextHygieneV061Tests(unittest.TestCase):
    def test_task_context_classifies_fresh_continued_reused_and_cross_role(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {"CODEX_THREAD_ID": "thread-private-1"}
            with mock.patch.dict(os.environ, environment, clear=False):
                first = bind_task_context(
                    root,
                    change_id="C001",
                    role="implementation",
                    cycle_ref="cycle-a",
                    operation_id="op-a",
                )
                continued = bind_task_context(
                    root,
                    change_id="C001",
                    role="implementation",
                    cycle_ref="cycle-a",
                    operation_id="op-a-retry",
                )
                reused = bind_task_context(
                    root,
                    change_id="C001",
                    role="implementation",
                    cycle_ref="cycle-b",
                    operation_id="op-b",
                )
                cross_role = bind_task_context(
                    root,
                    change_id="C001",
                    role="review",
                    cycle_ref="review-a",
                    operation_id="review-a",
                )
            self.assertEqual(first["status"], "fresh")
            self.assertEqual(continued["status"], "continued")
            self.assertEqual(reused["status"], "reused")
            self.assertEqual(reused["reuse_reason"], "same-role-new-cycle")
            self.assertEqual(reused["recommendation"], "open-fresh-task")
            self.assertEqual(cross_role["status"], "reused")
            self.assertEqual(cross_role["reuse_reason"], "cross-role")
            public = json.dumps([first, continued, reused, cross_role])
            self.assertNotIn("thread-private-1", public)

    def test_new_thread_is_fresh_and_missing_thread_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cycle = task_cycle_ref(role="review", components={"review": "one"})
            with mock.patch.dict(
                os.environ, {"CODEX_THREAD_ID": "thread-one"}, clear=False
            ):
                bind_task_context(
                    root,
                    change_id="C001",
                    role="review",
                    cycle_ref=cycle,
                    operation_id="one",
                )
            with mock.patch.dict(
                os.environ, {"CODEX_THREAD_ID": "thread-two"}, clear=False
            ):
                fresh = bind_task_context(
                    root,
                    change_id="C001",
                    role="review",
                    cycle_ref=cycle,
                    operation_id="two",
                )
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CODEX_THREAD_ID", None)
                unavailable = review_task_context(
                    root,
                    change_id="C001",
                    operation_id="missing",
                    review_id="review-id",
                    pack_digest="pack-digest",
                    record=True,
                )
            self.assertEqual(fresh["status"], "fresh")
            self.assertEqual(unavailable["status"], "unavailable")

    def test_descendant_candidate_in_same_manifest_is_one_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(
                os.environ, {"CODEX_THREAD_ID": "thread-candidate"}, clear=False
            ):
                first = candidate_task_context(
                    root,
                    change_id="C001",
                    operation_id="head-a",
                    definition_digest="definition",
                    review_base_sha="base",
                    canonical_review_id="review",
                    canonical_review_result_digest="result",
                    remediation_manifest_digest="manifest",
                    record=True,
                )
                descendant = candidate_task_context(
                    root,
                    change_id="C001",
                    operation_id="head-b",
                    definition_digest="definition",
                    review_base_sha="base",
                    canonical_review_id="review",
                    canonical_review_result_digest="result",
                    remediation_manifest_digest="manifest",
                    record=True,
                )
            self.assertEqual(first["status"], "fresh")
            self.assertEqual(descendant["status"], "continued")

    def test_concurrent_binding_is_single_and_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(
                os.environ, {"CODEX_THREAD_ID": "thread-concurrent"}, clear=False
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(
                        executor.map(
                            lambda index: bind_task_context(
                                root,
                                change_id="C001",
                                role="review",
                                cycle_ref="cycle",
                                operation_id=f"op-{index}",
                            ),
                            range(2),
                        )
                    )
            self.assertEqual(
                sorted(item["status"] for item in results),
                ["continued", "fresh"],
            )
            files = list((root / ".dls/cache/telemetry/C001/tasks").glob("*.json"))
            self.assertEqual(len(files), 1)

            unsafe_root = root / "unsafe"
            unsafe_root.mkdir()
            (unsafe_root / ".dls").mkdir()
            (unsafe_root / ".dls/cache").symlink_to(root / "outside")
            with mock.patch.dict(
                os.environ, {"CODEX_THREAD_ID": "thread-unsafe"}, clear=False
            ):
                with self.assertRaises(IntegrityError):
                    bind_task_context(
                        unsafe_root,
                        change_id="C001",
                        role="review",
                        cycle_ref="cycle",
                        operation_id="unsafe",
                    )

    def test_remediation_statuses_share_exact_head_pack_and_prior_review(self) -> None:
        helper = candidate_tests.CandidateRunnerV040Tests(methodName="runTest")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _, first_pack = helper._import_findings(root, ("C001-R001",))
            (root / "README.md").write_text("# Fixture\n\nRemediated.\n", encoding="utf-8")
            git(root, "add", "README.md")
            git(root, "commit", "-m", "remediate finding")
            prepared = candidate_ready(
                root,
                change_id="C001",
                base_ref=None,
                addressed=["C001-R001"],
                noted=[],
                extra_commands=[],
                operation_id=None,
            )
            pack = json.loads(
                (root / prepared["review_pack_path"]).read_text(encoding="utf-8")
            )
            candidate = candidate_status(root, change_id="C001")
            review = review_status(root, change_id="C001")
            delivery = delivery_status(root, change_id="C001")
            self.assertEqual(review["candidate_head"], pack["head_sha"])
            self.assertEqual(review["prior_review_id"], first_pack["review_id"])
            self.assertEqual(
                review["prior_review_result_path"],
                pack["prior_review"]["result_path"],
            )
            self.assertEqual(
                review["prior_remediation_manifest_path"],
                pack["prior_review"]["remediation_manifest_path"],
            )
            self.assertEqual(
                review["prior_reviewed_head"], pack["prior_review"]["head_sha"]
            )
            self.assertTrue(review["exact_head"])
            self.assertTrue(review["prepared"])
            self.assertEqual(candidate["review_id"], review["review_id"])
            self.assertEqual(delivery["candidate"]["review_id"], review["review_id"])
            self.assertEqual(delivery["review"]["review_id"], review["review_id"])
            self.assertEqual(delivery["next_action"]["id"], "start-review")

    def test_review_stream_warns_once_before_models_when_task_is_reused(self) -> None:
        candidate_helper = candidate_tests.CandidateRunnerV040Tests(methodName="runTest")
        review_helper = review_tests.ReviewRunnerV030Tests(methodName="runTest")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(
                os.environ, {"CODEX_THREAD_ID": "one-long-task"}, clear=False
            ):
                base = candidate_helper._initial_candidate(root)
                candidate_ready(
                    root,
                    change_id="C001",
                    base_ref=base,
                    addressed=[],
                    noted=[],
                    extra_commands=[],
                    operation_id=None,
                )
                original, _, _ = review_helper._install_fake_codex(root)
                events: list[dict] = []
                try:
                    result = review_run(
                        root,
                        change_id="C001",
                        pack_path=None,
                        operation_id="review-in-same-task",
                        stream_callback=events.append,
                    )
                finally:
                    review_helper._restore_path(original)
            warnings = [item for item in events if item.get("event") == "context-warning"]
            self.assertEqual(len(warnings), 1)
            self.assertEqual(warnings[0]["reuse_reason"], "cross-role")
            self.assertEqual(result["task_context"]["status"], "reused")
            self.assertEqual(result["task_context"]["recommendation"], "open-fresh-task")
            self.assertNotIn(
                "one-long-task",
                json.dumps(StateStore(root).load("C001")),
            )
            for artifact in (root / ".dls/reviews/C001").rglob("*.json"):
                self.assertNotIn("one-long-task", artifact.read_text(encoding="utf-8"))

    def test_metrics_report_zero_native_context_and_controller_without_content(self) -> None:
        helper = candidate_tests.CandidateRunnerV040Tests(methodName="runTest")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            helper._initial_candidate(root)
            context_path = root / ".dls/cache/context.json"
            context_path.parent.mkdir(parents=True, exist_ok=True)
            context_path.write_text(
                json.dumps(
                    {
                        "context_mode": "compact",
                        "totals": {"bytes": 154538, "words": 12302},
                        "inputs": [
                            {"reason": "active-review-pack"},
                            {"reason": "ticket-definition"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            native_transcript = root / ".dls/cache/native.jsonl"
            native_transcript.write_text(
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 0,
                            "cached_input_tokens": 0,
                            "output_tokens": 0,
                            "reasoning_output_tokens": 0,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            state = StateStore(root).load("C001")

            def mutate(value: dict) -> None:
                value["reviews"].extend(
                    [
                        {
                            "kind": "native",
                            "review_id": "pilot",
                            "lane_key": "native",
                            "status": "completed",
                            "model": "gpt-5.6-terra",
                            "reasoning_effort": "high",
                            "attempt_ordinal": 1,
                            "command_events": 2,
                            "transcript_path": ".dls/cache/native.jsonl",
                            "context_manifest_path": ".dls/cache/context.json",
                        },
                        {
                            "kind": "semantic",
                            "review_id": "pilot",
                            "lane_key": "semantic:targeted",
                            "status": "completed",
                            "model": "gpt-5.6-sol",
                            "reasoning_effort": "xhigh",
                            "attempt_ordinal": 1,
                            "command_events": 10,
                            "context_manifest_path": ".dls/cache/context.json",
                            "usage": {
                                "input_tokens": 3_300_000,
                                "cached_input_tokens": 3_000_000,
                                "output_tokens": 49_295,
                                "reasoning_output_tokens": 10_000,
                            },
                        },
                    ]
                )

            StateStore(root).mutate(
                "C001",
                expected_revision=state["state_revision"],
                operation_id="pilot-metrics",
                operation_kind="fixture",
                mutator=mutate,
            )
            rollout = root / "rollout.jsonl"
            events = [
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn"}},
                {"type": "response_item", "payload": {"type": "message", "content": "SECRET"}},
                {"type": "response_item", "payload": {"type": "function_call", "arguments": "SECRET"}},
                {"type": "response_item", "payload": {"type": "function_call_output", "output": "SECRET"}},
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 1_000_000,
                                "cached_input_tokens": 900_000,
                                "output_tokens": 33_592,
                                "reasoning_output_tokens": 12_000,
                            }
                        },
                    },
                },
                {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn"}},
            ]
            rollout.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            telemetry = root / ".dls/cache/telemetry/C001/pilot.json"
            telemetry.parent.mkdir(parents=True, exist_ok=True)
            telemetry.write_text(
                json.dumps(
                    {
                        "contract": METRICS_CONTRACT,
                        "thread_id": "private-thread",
                        "thread_ref": "safe-ref",
                        "turn_id": "turn",
                        "baseline_usage": None,
                        "task_context": {
                            "contract": TASK_CONTEXT_CONTRACT,
                            "status": "reused",
                            "role": "review",
                            "reuse_reason": "same-role-new-cycle",
                            "prior_cycle_count": 1,
                            "recommendation": "open-fresh-task",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("dls_core.telemetry._find_rollout", return_value=rollout):
                metrics = review_metrics(
                    root,
                    change_id="C001",
                    review_id="pilot",
                    verbose=True,
                )
            native = next(item for item in metrics["lanes"] if item["lane"] == "native")
            targeted = next(
                item for item in metrics["lanes"] if item["lane"] == "semantic:targeted"
            )
            self.assertIsNone(native["usage"])
            self.assertEqual(native["usage_source"], "reported-zero")
            self.assertIn("native-reported-zero", metrics["completeness_reasons"])
            self.assertEqual(targeted["context"]["bytes"], 154538)
            self.assertEqual(targeted["context"]["words"], 12302)
            self.assertEqual(metrics["child_usage"]["processed_tokens"], 3_349_295)
            self.assertEqual(metrics["controller"]["usage"]["processed_tokens"], 1_033_592)
            self.assertEqual(metrics["all_in"]["usage"]["processed_tokens"], 4_382_887)
            self.assertEqual(metrics["controller"]["event_counts"]["model_messages"], 1)
            self.assertEqual(metrics["controller"]["event_counts"]["tool_calls"], 1)
            self.assertEqual(metrics["controller"]["event_counts"]["tool_outputs"], 1)
            self.assertEqual(metrics["controller"]["event_counts"]["token_samples"], 1)
            encoded = json.dumps(metrics)
            self.assertNotIn("SECRET", encoded)
            self.assertNotIn("private-thread", encoded)


if __name__ == "__main__":
    unittest.main()
