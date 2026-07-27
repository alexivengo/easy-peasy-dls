from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from dls_core.economy import (
    DEFAULT_REVIEW_BUDGETS,
    ReviewBudget,
    processed_tokens,
    review_budget,
    token_budget_failure,
)
from dls_core.operations import _run_bounded_command
from dls_core.repo import allowed_environment
from dls_core.state import StateStore
from dls_core.telemetry import (
    _normalize_usage,
    _sum_usage,
    _codex_task_usage,
    cache_prune,
    cache_status,
    delivery_status,
    record_review_task_reference,
    review_metrics,
)

from support import create_change, initialize, initialize_git


class EconomyV050Tests(unittest.TestCase):
    def _root(self, directory: str, *, control: str = "critical") -> Path:
        root = Path(directory)
        initialize_git(root)
        initialize(root)
        create_change(root, control=control)
        return root

    def test_usage_arithmetic_does_not_double_count_cached_input(self) -> None:
        first = _normalize_usage(
            {
                "input_tokens": 100,
                "cached_input_tokens": 80,
                "output_tokens": 20,
                "reasoning_output_tokens": 5,
            }
        )
        second = _normalize_usage(
            {"input_tokens": 50, "cached_input_tokens": 10, "output_tokens": 5}
        )
        assert first is not None and second is not None
        total = _sum_usage([first, second])
        self.assertEqual(total["input_tokens"], 150)
        self.assertEqual(total["cached_input_tokens"], 90)
        self.assertEqual(total["uncached_input_tokens"], 60)
        self.assertEqual(total["processed_tokens"], 175)
        self.assertEqual(processed_tokens(total), 175)
        self.assertIsNone(_normalize_usage({"input_tokens": 0, "output_tokens": 0}))

    def test_sanitized_epic_fixture_reports_8264764_child_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            state = StateStore(root).load("C001")

            def mutate(value: dict) -> None:
                for lane, input_tokens, output_tokens in (
                    ("semantic:targeted", 4_270_000, 24_245),
                    ("reconciliation", 1_620_000, 12_244),
                    ("semantic:final-full", 2_333_497, 4_778),
                ):
                    value["reviews"].append(
                        {
                            "kind": "semantic",
                            "review_id": "review-fixture",
                            "lane_key": lane,
                            "status": "completed",
                            "model": "gpt-5.6-sol",
                            "reasoning_effort": "xhigh",
                            "attempt_ordinal": 1,
                            "usage": {
                                "input_tokens": input_tokens,
                                "cached_input_tokens": input_tokens - 10_000,
                                "output_tokens": output_tokens,
                                "reasoning_output_tokens": 1_000,
                            },
                        }
                    )

            StateStore(root).mutate(
                "C001",
                expected_revision=state["state_revision"],
                operation_id="metrics-fixture",
                operation_kind="fixture",
                mutator=mutate,
            )
            result = review_metrics(
                root, change_id="C001", review_id="review-fixture"
            )
            self.assertEqual(result["child_usage"]["processed_tokens"], 8_264_764)
            self.assertEqual(result["usage_status"], "complete")
            self.assertEqual(result["all_in"]["kind"], "lower-bound")
            encoded = json.dumps(result)
            self.assertNotIn("prompt", encoded)
            self.assertNotIn("raw_output", encoded)

    def test_metrics_include_every_retry_and_mark_missing_native_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            state = StateStore(root).load("C001")

            def mutate(value: dict) -> None:
                value["reviews"].extend(
                    [
                        {
                            "kind": "native",
                            "review_id": "review-retries",
                            "lane_key": "native",
                            "status": "completed",
                            "model": "gpt-5.6-terra",
                            "attempt_ordinal": 1,
                            "usage": {"input_tokens": 0, "output_tokens": 0},
                        },
                        {
                            "kind": "semantic",
                            "review_id": "review-retries",
                            "lane_key": "semantic:targeted",
                            "status": "api-failure",
                            "model": "gpt-5.6-sol",
                            "attempt_ordinal": 1,
                            "usage": {"input_tokens": 100, "output_tokens": 10},
                        },
                        {
                            "kind": "semantic",
                            "review_id": "review-retries",
                            "lane_key": "semantic:targeted",
                            "status": "completed",
                            "model": "gpt-5.6-sol",
                            "attempt_ordinal": 2,
                            "usage": {"input_tokens": 200, "output_tokens": 20},
                        },
                    ]
                )

            StateStore(root).mutate(
                "C001",
                expected_revision=state["state_revision"],
                operation_id="retry-metrics",
                operation_kind="fixture",
                mutator=mutate,
            )
            result = review_metrics(
                root, change_id="C001", review_id="review-retries"
            )
            self.assertEqual(result["usage_status"], "partial")
            self.assertEqual(result["child_usage"]["processed_tokens"], 330)
            self.assertEqual(len(result["lanes"]), 3)
            self.assertTrue(result["lanes"][-1]["retry"])
            self.assertIsNone(result["lanes"][0]["usage"])

    def test_codex_adapter_uses_current_turn_delta_and_filters_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            rollout = Path(directory) / "rollout.jsonl"
            events = [
                {
                    "timestamp": "2026-07-27T10:00:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 800,
                                "output_tokens": 100,
                                "reasoning_output_tokens": 50,
                            }
                        },
                    },
                },
                {
                    "timestamp": "2026-07-27T10:01:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "turn-2"},
                },
                {
                    "timestamp": "2026-07-27T10:01:01Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "SECRET"},
                },
                {
                    "timestamp": "2026-07-27T10:02:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 1400,
                                "cached_input_tokens": 1100,
                                "output_tokens": 160,
                                "reasoning_output_tokens": 70,
                            }
                        },
                    },
                },
                {
                    "timestamp": "2026-07-27T10:03:00Z",
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": "turn-2"},
                },
            ]
            rollout.write_text(
                "".join(json.dumps(item) + "\n" for item in events),
                encoding="utf-8",
            )
            usage, completed = _codex_task_usage(
                rollout,
                turn_id="turn-2",
                baseline_usage=_normalize_usage(
                    {
                        "input_tokens": 1000,
                        "cached_input_tokens": 800,
                        "output_tokens": 100,
                        "reasoning_output_tokens": 50,
                    }
                ),
            )
            self.assertTrue(completed)
            assert usage is not None
            self.assertEqual(usage["processed_tokens"], 460)
            self.assertNotIn("SECRET", json.dumps(usage))

    def test_completed_controller_turn_produces_exact_all_in_total(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            state = StateStore(root).load("C001")

            def mutate(value: dict) -> None:
                value["reviews"].append(
                    {
                        "kind": "semantic",
                        "review_id": "review-exact",
                        "lane_key": "semantic:full",
                        "status": "completed",
                        "model": "gpt-5.6-sol",
                        "attempt_ordinal": 1,
                        "usage": {"input_tokens": 10, "output_tokens": 2},
                    }
                )

            StateStore(root).mutate(
                "C001",
                expected_revision=state["state_revision"],
                operation_id="exact-metrics",
                operation_kind="fixture",
                mutator=mutate,
            )
            telemetry = root / ".dls/cache/telemetry/C001/review-exact.json"
            telemetry.parent.mkdir(parents=True, exist_ok=True)
            telemetry.write_text(
                json.dumps(
                    {
                        "thread_id": "thread-exact",
                        "thread_ref": "hashed-thread",
                        "turn_id": "turn-exact",
                        "baseline_usage": _normalize_usage(
                            {"input_tokens": 100, "output_tokens": 10}
                        ),
                    }
                ),
                encoding="utf-8",
            )
            rollout = root / ".dls/cache/telemetry/rollout.jsonl"
            rollout.write_text(
                "\n".join(
                    json.dumps(item)
                    for item in (
                        {
                            "type": "event_msg",
                            "payload": {"type": "task_started", "turn_id": "turn-exact"},
                        },
                        {
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "total_token_usage": {
                                        "input_tokens": 140,
                                        "output_tokens": 15,
                                    }
                                },
                            },
                        },
                        {
                            "type": "event_msg",
                            "payload": {"type": "task_complete", "turn_id": "turn-exact"},
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch("dls_core.telemetry._find_rollout", return_value=rollout):
                result = review_metrics(
                    root, change_id="C001", review_id="review-exact"
                )
            self.assertEqual(result["all_in"]["kind"], "exact")
            self.assertEqual(result["controller"]["usage"]["processed_tokens"], 45)
            self.assertEqual(result["all_in"]["usage"]["processed_tokens"], 57)
            self.assertLess(len(json.dumps(result).encode("utf-8")), 12288)

    def test_task_reference_keeps_raw_id_only_in_ignored_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "private-thread"}), mock.patch(
                "dls_core.telemetry._find_rollout", return_value=None
            ):
                record_review_task_reference(
                    root,
                    change_id="C001",
                    review_id="review-private",
                    operation_id="operation-private",
                )
            private = json.loads(
                (root / ".dls/cache/telemetry/C001/review-private.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(private["thread_id"], "private-thread")
            public = review_metrics(
                root, change_id="C001", review_id="review-private"
            )
            self.assertNotIn("private-thread", json.dumps(public))

    def test_delivery_status_prefers_imported_review_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            with mock.patch(
                "dls_core.candidate_runner.candidate_status",
                return_value={
                    "status": "completed",
                    "phase": "completed",
                    "review_id": "review-clear",
                    "next_action": {"id": "open-review-task", "detail": "pack"},
                },
            ), mock.patch(
                "dls_core.review_runner.review_status",
                return_value={
                    "status": "completed",
                    "review_id": "review-clear",
                    "verdict": "review-clear",
                    "review_result_path": ".dls/reviews/C001/results/clear.json",
                    "next_action": {"id": "accept-review", "detail": "result"},
                },
            ):
                result = delivery_status(root, change_id="C001")
            self.assertEqual(result["next_action"]["id"], "accept-review")
            self.assertLess(len(json.dumps(result).encode("utf-8")), 2048)

    def test_budget_defaults_and_repository_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, control="standard")
            self.assertEqual(review_budget(root, "standard"), DEFAULT_REVIEW_BUDGETS["standard"])
            config = root / ".dls/config.toml"
            config.write_text(
                config.read_text(encoding="utf-8")
                + "\n[review_budgets.standard]\nlane_tokens = 123456\n",
                encoding="utf-8",
            )
            self.assertEqual(review_budget(root, "standard").lane_tokens, 123456)

    def test_lane_and_aggregate_token_budgets_are_distinct(self) -> None:
        budget = ReviewBudget(
            aggregate_tokens=100,
            lane_tokens=75,
            command_events=5,
            timeout_seconds=5,
            transcript_bytes=1024,
        )
        self.assertIn(
            "lane processed_tokens=80",
            token_budget_failure(
                {"input_tokens": 70, "output_tokens": 10},
                aggregate_before=0,
                budget=budget,
            )
            or "",
        )
        self.assertIn(
            "aggregate processed_tokens=110",
            token_budget_failure(
                {"input_tokens": 50, "output_tokens": 10},
                aggregate_before=50,
                budget=budget,
            )
            or "",
        )

    def test_bounded_runner_stops_after_command_event_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                "import json\n"
                "for i in range(10):\n"
                " print(json.dumps({'type':'item.completed','item':{'type':'command_execution'}}), flush=True)\n"
            )
            result = _run_bounded_command(
                [os.environ.get("PYTHON", "python3"), "-c", script],
                cwd=root,
                environment=allowed_environment([]),
                timeout_seconds=5,
                max_output_bytes=64 * 1024,
                max_command_events=2,
            )
            self.assertTrue(result["budget_exceeded"])
            self.assertGreater(result["command_events"], 2)

    def test_bounded_runner_enforces_duration_and_transcript_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            timed = _run_bounded_command(
                [os.environ.get("PYTHON", "python3"), "-c", "import time; time.sleep(2)"],
                cwd=root,
                environment=allowed_environment([]),
                timeout_seconds=1,
                max_output_bytes=1024,
            )
            self.assertTrue(timed["timed_out"])
            overflowing = _run_bounded_command(
                [os.environ.get("PYTHON", "python3"), "-c", "print('x' * 4096)"],
                cwd=root,
                environment=allowed_environment([]),
                timeout_seconds=5,
                max_output_bytes=1024,
            )
            self.assertTrue(overflowing["overflow"])
            self.assertEqual(overflowing["exit_code"], 125)
            self.assertEqual(len(overflowing["output"]), 1024)

    def test_cache_prune_is_dry_run_and_preserves_recent_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            cache = root / ".dls/cache/reviews/C001/old-review"
            cache.mkdir(parents=True)
            old = cache / "old.jsonl"
            old.write_text("old", encoding="utf-8")
            timestamp = (
                datetime.now(timezone.utc) - timedelta(days=20)
            ).timestamp()
            os.utime(old, (timestamp, timestamp))
            recent = cache / "recent.jsonl"
            recent.write_text("recent", encoding="utf-8")
            preview = cache_prune(root, change_id="C001", apply=False)
            self.assertIn(str(old.relative_to(root)), preview["paths"])
            self.assertTrue(old.exists())
            applied = cache_prune(root, change_id="C001", apply=True)
            self.assertTrue(applied["changed"])
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertGreater(cache_status(root, change_id="C001")["bytes"], 0)

    def test_cache_rejects_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory)
            cache = root / ".dls/cache/reviews/C001"
            cache.mkdir(parents=True, exist_ok=True)
            target = root / "outside.txt"
            target.write_text("outside", encoding="utf-8")
            (cache / "escape").symlink_to(target)
            with self.assertRaisesRegex(Exception, "symlink"):
                cache_status(root, change_id="C001")


if __name__ == "__main__":
    unittest.main()
