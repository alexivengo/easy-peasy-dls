# L0 evaluation claim map

This map is traceability evidence. The existing DLS suite executes its named
tests; the public validator rejects a missing claim, oracle, or discovered test
ID. L0 makes no live call through the supported `codex` subprocess route.

## HC-01

Hard oracle: Before/after state digest is identical

- `test_core_reset_v011.CoreResetTests.test_acceptance_is_separate_and_exact_head`
- `test_core_reset_v011.CoreResetTests.test_stale_human_decision_cannot_accept_new_head`

## HC-02

Hard oracle: Caller/foreign worktree diff is identical

- `test_core_reset_v011.CoreResetTests.test_execution_context_prepares_owner_and_leaves_dirty_caller_untouched`
- `test_core_reset_v011.CoreResetTests.test_dirty_main_routes_candidate_and_review_to_clean_owner`
- `test_core_reset_v011.CoreResetTests.test_dirty_owner_stops_before_product_work`
- `test_core_reset_v011.CoreResetTests.test_second_state_bearing_owner_is_an_explicit_conflict`

## HC-03

Hard oracle: HEAD/tree/policy/profile digests match

- `test_core_reset_v011.CoreResetTests.test_exact_head_evidence_and_invalidation`
- `test_core_reset_v011.CoreResetTests.test_descendant_candidate_reuses_preserved_base_and_rejects_conflict`
- `test_core_reset_v011.CoreResetTests.test_profile_drift_invalidates_candidate`
- `test_core_reset_v011.CoreResetTests.test_validation_failure_never_creates_pack`

## HC-04

Hard oracle: terminal=true and review_result_path != null

- `test_core_reset_v011.CoreResetTests.test_stream_events_distinguish_running_from_terminal`

## HC-05A

Hard oracle: No bypass; continuation count <= contract

- `test_task_guard.TaskGuardTests.test_dirty_owner_consent_yes_rearms_guard`
- `test_task_guard.TaskGuardTests.test_dirty_owner_consent_no_clears_guard`
- `test_task_guard.TaskGuardTests.test_changed_draft_does_not_reuse_stale_consent`

## HC-05B

Hard oracle: No bypass; continuation count <= contract

- `test_task_guard.TaskGuardTests.test_two_continuations_then_terminal_bounded_diagnostic`
- `test_task_guard.TaskGuardTests.test_git_churn_never_resets_absolute_budget`
- `test_task_guard.TaskGuardTests.test_real_progress_does_not_expand_absolute_budget`
