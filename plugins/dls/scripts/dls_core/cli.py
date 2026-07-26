"""Command-line interface for the DLS kernel."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import VERSION
from .candidate_runner import candidate_ready, candidate_status
from .errors import DLSError, UsageError
from .operations import (
    adopt_change,
    approve,
    build_context,
    check,
    doctor,
    evidence_add,
    finding_disposition,
    init_repository,
    new_change,
    remediation_recover,
    remediation_start,
    review_import,
    review_pack,
    review_ready,
    review_start,
    revoke_approval,
    status,
    ticket_set,
    validate_command,
)
from .repo import find_repo_root
from .review_runner import review_run, review_status
from .worktrees import (
    worktree_list,
    worktree_register,
    worktree_unregister,
    worktree_verify,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dls",
        description="Governed, risk-adaptive delivery workflow kernel.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--root", type=Path, help="Repository root (defaults to auto-discovery).")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit stable JSON.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize repository-local DLS state.")
    _dry_run(init_parser)

    subparsers.add_parser("doctor", help="Inspect plugin and repository readiness.")

    new_parser = subparsers.add_parser("new", help="Create the minimal change package.")
    new_parser.add_argument("change_id")
    new_parser.add_argument("--slug", required=True)
    new_parser.add_argument("--title", required=True)
    new_parser.add_argument(
        "--kind",
        required=True,
        choices=("feature", "bug", "chore", "spike", "hotfix"),
    )
    new_parser.add_argument(
        "--control",
        required=True,
        choices=("micro", "routine", "standard", "critical"),
    )
    new_parser.add_argument("--impact", action="append", default=[])
    new_parser.add_argument("--roadmap-epic", action="store_true")
    new_parser.add_argument("--with-tickets", action="store_true")
    new_parser.add_argument("--with-adr", action="store_true")
    new_parser.add_argument("--outcome", required=True)
    _operation_id(new_parser)
    _dry_run(new_parser)

    adopt_parser = subparsers.add_parser(
        "adopt",
        help="Register a compatible existing package without rewriting it.",
    )
    adopt_parser.add_argument("change_id")
    adopt_parser.add_argument("--slug", required=True)
    adopt_parser.add_argument(
        "--kind",
        required=True,
        choices=("feature", "bug", "chore", "spike", "hotfix"),
    )
    adopt_parser.add_argument(
        "--control",
        required=True,
        choices=("routine", "standard", "critical"),
    )
    adopt_parser.add_argument("--impact", action="append", default=[])
    adopt_parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        metavar="KEY=PATH",
        help="Existing repository-relative artifact; repeat for each file.",
    )
    adopt_parser.add_argument(
        "--ticket-status",
        action="append",
        default=[],
        metavar="ID=STATUS",
        help="Current status for an existing ticket; repeat for every declared ticket.",
    )
    adopt_parser.add_argument(
        "--requirement-prefix",
        action="append",
        default=[],
        metavar="PREFIX",
        help="Existing requirement prefix such as F, N, or C.",
    )
    _operation_id(adopt_parser)
    _dry_run(adopt_parser)

    worktree_parser = subparsers.add_parser(
        "worktree",
        help="Manage explicit change-to-worktree routing for this Git repository.",
    )
    worktree_subparsers = worktree_parser.add_subparsers(
        dest="worktree_command",
        required=True,
    )
    worktree_register_parser = worktree_subparsers.add_parser(
        "register",
        help="Bind a change ID to an existing DLS worktree.",
    )
    worktree_register_parser.add_argument("change_id")
    worktree_register_parser.add_argument("owner_path", type=Path)
    _dry_run(worktree_register_parser)
    worktree_subparsers.add_parser(
        "list",
        help="List registered worktrees and their current validity.",
    )
    worktree_verify_parser = worktree_subparsers.add_parser(
        "verify",
        help="Verify one registered worktree.",
    )
    worktree_verify_parser.add_argument("change_id")
    worktree_unregister_parser = worktree_subparsers.add_parser(
        "unregister",
        help="Remove one local change-to-worktree binding.",
    )
    worktree_unregister_parser.add_argument("change_id")
    _dry_run(worktree_unregister_parser)

    status_parser = subparsers.add_parser("status", help="Show derived change status.")
    status_parser.add_argument("change_id")

    check_parser = subparsers.add_parser("check", help="Run deterministic gate checks.")
    check_parser.add_argument("change_id")
    check_parser.add_argument(
        "--gate",
        default="all",
        choices=("definition", "review", "accept", "all"),
    )

    context_parser = subparsers.add_parser("context", help="Build a digest-bound context manifest.")
    context_parser.add_argument("change_id")
    context_parser.add_argument(
        "--phase",
        required=True,
        choices=("implementation", "review", "remediation"),
    )
    context_parser.add_argument("--include", action="append", default=[])
    context_parser.add_argument("--exclude", action="append", default=[])
    _dry_run(context_parser)

    approve_parser = subparsers.add_parser("approve", help="Record a scoped human decision.")
    approve_parser.add_argument("change_id")
    approve_action = approve_parser.add_mutually_exclusive_group(required=True)
    approve_action.add_argument(
        "--decision",
        choices=("definition", "accept", "exception", "design", "architecture"),
    )
    approve_action.add_argument("--revoke", metavar="APPROVAL_ID")
    approve_parser.add_argument("--actor", required=True, choices=("codex", "user"))
    approve_parser.add_argument("--prompt")
    approve_parser.add_argument("--response")
    approve_parser.add_argument("--git-sha")
    approve_parser.add_argument("--conditions")
    approve_parser.add_argument("--rationale")
    _revision(approve_parser)
    _operation_id(approve_parser)
    _dry_run(approve_parser)

    ticket_parser = subparsers.add_parser("ticket", help="Manage ticket execution state.")
    ticket_subparsers = ticket_parser.add_subparsers(dest="ticket_command", required=True)
    ticket_set_parser = ticket_subparsers.add_parser("set", help="Set a legal ticket state.")
    ticket_set_parser.add_argument("change_id")
    ticket_set_parser.add_argument("ticket_id")
    ticket_set_parser.add_argument(
        "ticket_status",
        choices=(
            "planned",
            "ready",
            "in-progress",
            "implemented",
            "validated",
            "blocked",
            "done",
        ),
    )
    ticket_set_parser.add_argument("--note")
    _revision(ticket_set_parser)
    _operation_id(ticket_set_parser)
    _dry_run(ticket_set_parser)

    evidence_parser = subparsers.add_parser("evidence", help="Import validation evidence.")
    evidence_subparsers = evidence_parser.add_subparsers(dest="evidence_command", required=True)
    evidence_add_parser = evidence_subparsers.add_parser("add", help="Add immutable evidence.")
    evidence_add_parser.add_argument("change_id")
    evidence_add_parser.add_argument("--command-id", required=True)
    evidence_add_parser.add_argument("--exit-code", required=True, type=int)
    evidence_add_parser.add_argument("--summary", required=True)
    evidence_add_parser.add_argument("--git-sha")
    evidence_add_parser.add_argument("--artifact", action="append", default=[])
    evidence_add_parser.add_argument("--environment")
    evidence_add_parser.add_argument("--duration-seconds", type=float)
    _revision(evidence_add_parser)
    _operation_id(evidence_add_parser)
    _dry_run(evidence_add_parser)

    review_pack_parser = subparsers.add_parser(
        "review-pack",
        help="Create an immutable exact-revision review handoff.",
    )
    review_pack_parser.add_argument("change_id")
    review_pack_parser.add_argument("--base", required=True)
    review_pack_parser.add_argument("--head")
    review_pack_parser.add_argument("--advisory-dirty", action="store_true")
    _revision(review_pack_parser)
    _operation_id(review_pack_parser)
    _dry_run(review_pack_parser)

    remediation_start_parser = subparsers.add_parser(
        "remediation-start",
        help="Build a latest-only digest-bound remediation manifest.",
    )
    remediation_start_parser.add_argument("change_id")
    _dry_run(remediation_start_parser)

    remediation_recover_parser = subparsers.add_parser(
        "remediation-recover",
        help="Recover a missing canonical remediation manifest from exact Git objects.",
    )
    remediation_recover_parser.add_argument("change_id")
    remediation_recover_parser.add_argument("--review-id")
    _operation_id(remediation_recover_parser)
    _dry_run(remediation_recover_parser)

    candidate_ready_parser = subparsers.add_parser(
        "candidate-ready",
        help="Validate and atomically prepare an implementation candidate for review.",
    )
    candidate_ready_parser.add_argument("change_id")
    candidate_ready_parser.add_argument(
        "--base",
        help="Required for the first review; inferred from canonical ReviewIR for remediation.",
    )
    candidate_ready_parser.add_argument("--address", action="append", default=[])
    candidate_ready_parser.add_argument("--note", action="append", default=[])
    candidate_ready_parser.add_argument("--extra-command", action="append", default=[])
    _operation_id(candidate_ready_parser)
    _dry_run(candidate_ready_parser)

    candidate_status_parser = subparsers.add_parser(
        "candidate-status",
        help="Read compact implementation candidate telemetry without running commands.",
    )
    candidate_status_parser.add_argument("change_id")
    _operation_id(candidate_status_parser)

    review_ready_parser = subparsers.add_parser(
        "review-ready",
        help="Check a candidate and create the next full or remediation ReviewPack.",
    )
    review_ready_parser.add_argument("change_id")
    review_ready_parser.add_argument(
        "--base",
        help="Required for the first review; inferred from the latest ReviewIR for remediation.",
    )
    _revision(review_ready_parser)
    _operation_id(review_ready_parser)
    _dry_run(review_ready_parser)

    review_start_parser = subparsers.add_parser(
        "review-start",
        help="Start or resume the single-flight native lane for one ReviewPack.",
    )
    review_start_parser.add_argument("change_id")
    review_start_parser.add_argument(
        "--pack",
        help=(
            "Repository-relative ReviewPack, or an absolute ReviewPack path "
            "to select its owner checkout explicitly."
        ),
    )
    _operation_id(review_start_parser)
    _dry_run(review_start_parser)

    review_run_parser = subparsers.add_parser(
        "review-run",
        help="Run native, specialist, semantic, reconciliation, and import end to end.",
    )
    review_run_parser.add_argument("change_id")
    review_run_parser.add_argument(
        "--pack",
        help=(
            "Repository-relative ReviewPack, or an absolute ReviewPack path "
            "to select its owner checkout explicitly."
        ),
    )
    _operation_id(review_run_parser)
    _dry_run(review_run_parser)

    review_status_parser = subparsers.add_parser(
        "review-status",
        help="Read one review pipeline state without launching a model.",
    )
    review_status_parser.add_argument("change_id")
    review_status_parser.add_argument("--review-id")
    review_status_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include full lane attempts, argv, paths, and provenance details.",
    )

    review_import_parser = subparsers.add_parser(
        "review-import",
        help="Validate and import a ReviewIR result.",
    )
    review_import_parser.add_argument("change_id")
    review_import_parser.add_argument("report_path")
    _revision(review_import_parser)
    _operation_id(review_import_parser)
    _dry_run(review_import_parser)

    finding_parser = subparsers.add_parser(
        "finding",
        help="Record an addressed finding, waiver, reopen, or note.",
    )
    finding_subparsers = finding_parser.add_subparsers(dest="finding_command", required=True)
    finding_set_parser = finding_subparsers.add_parser("set")
    finding_set_parser.add_argument("change_id")
    finding_set_parser.add_argument("finding_id")
    finding_set_parser.add_argument(
        "disposition_status",
        choices=("addressed", "resolved", "waived", "reopened", "note"),
    )
    finding_set_parser.add_argument("--rationale", required=True)
    finding_set_parser.add_argument("--git-sha")
    finding_set_parser.add_argument(
        "--evidence",
        action="extend",
        nargs="+",
        default=[],
        metavar="PATH",
        help=(
            "Repository-relative DLS evidence path. Accepts multiple paths, "
            "comma-separated paths, or repeated --evidence flags."
        ),
    )
    finding_set_parser.add_argument("--actor", required=True, choices=("codex", "user"))
    finding_set_parser.add_argument("--prompt")
    finding_set_parser.add_argument("--response")
    _revision(finding_set_parser)
    _operation_id(finding_set_parser)
    _dry_run(finding_set_parser)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Run one trusted command declared in repository config.",
    )
    validate_parser.add_argument("change_id")
    validate_parser.add_argument("command_id")
    _revision(validate_parser)
    _operation_id(validate_parser)
    _dry_run(validate_parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    root = (arguments.root or find_repo_root(Path.cwd())).resolve()
    try:
        result = dispatch(root, arguments)
    except DLSError as exc:
        if arguments.as_json:
            print(
                json.dumps(
                    {"ok": False, "error": exc.__class__.__name__, "message": str(exc)},
                    sort_keys=True,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 3
    except OSError as exc:
        if arguments.as_json:
            print(
                json.dumps(
                    {"ok": False, "error": "OSError", "message": str(exc)},
                    sort_keys=True,
                    ensure_ascii=False,
                )
            )
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 3
    if arguments.as_json:
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(_human_result(arguments, result))
    return 0 if result.get("ok", False) else 1


def dispatch(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    command = args.command
    if command == "init":
        return init_repository(root, dry_run=args.dry_run)
    if command == "doctor":
        return doctor(root)
    if command == "new":
        return new_change(
            root,
            change_id=args.change_id,
            slug=args.slug,
            title=args.title,
            work_kind=args.kind,
            control_level=args.control,
            impact_tags=_split_values(args.impact),
            roadmap_epic=args.roadmap_epic,
            with_tickets=args.with_tickets,
            with_adr=args.with_adr,
            outcome=args.outcome,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "adopt":
        return adopt_change(
            root,
            change_id=args.change_id,
            slug=args.slug,
            work_kind=args.kind,
            control_level=args.control,
            impact_tags=_split_values(args.impact),
            artifacts=_key_value_pairs(args.artifact, "--artifact"),
            ticket_statuses=_key_value_pairs(
                args.ticket_status,
                "--ticket-status",
            ),
            requirement_prefixes=_split_values(args.requirement_prefix),
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "worktree" and args.worktree_command == "register":
        return worktree_register(
            root,
            change_id=args.change_id,
            owner_path=args.owner_path,
            dry_run=args.dry_run,
        )
    if command == "worktree" and args.worktree_command == "list":
        return worktree_list(root)
    if command == "worktree" and args.worktree_command == "verify":
        return worktree_verify(root, change_id=args.change_id)
    if command == "worktree" and args.worktree_command == "unregister":
        return worktree_unregister(
            root,
            change_id=args.change_id,
            dry_run=args.dry_run,
        )
    if command == "status":
        return status(root, change_id=args.change_id)
    if command == "check":
        return check(root, change_id=args.change_id, gate=args.gate)
    if command == "context":
        return build_context(
            root,
            change_id=args.change_id,
            phase=args.phase,
            include=args.include,
            exclude=args.exclude,
            dry_run=args.dry_run,
        )
    if command == "approve":
        if args.revoke:
            return revoke_approval(
                root,
                change_id=args.change_id,
                approval_id=args.revoke,
                expected_revision=args.expect_revision,
                actor=args.actor,
                prompt=args.prompt,
                response=args.response,
                rationale=args.rationale or args.conditions or "",
                operation_id=args.operation_id,
                dry_run=args.dry_run,
            )
        return approve(
            root,
            change_id=args.change_id,
            decision=args.decision,
            expected_revision=args.expect_revision,
            actor=args.actor,
            prompt=args.prompt,
            response=args.response,
            git_sha=args.git_sha,
            conditions=args.conditions,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "ticket" and args.ticket_command == "set":
        return ticket_set(
            root,
            change_id=args.change_id,
            ticket_id=args.ticket_id,
            ticket_status=args.ticket_status,
            expected_revision=args.expect_revision,
            note=args.note,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "evidence" and args.evidence_command == "add":
        return evidence_add(
            root,
            change_id=args.change_id,
            command_id=args.command_id,
            exit_code=args.exit_code,
            summary=args.summary,
            expected_revision=args.expect_revision,
            git_sha=args.git_sha,
            artifacts=args.artifact,
            environment=args.environment,
            duration_seconds=args.duration_seconds,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "review-pack":
        return review_pack(
            root,
            change_id=args.change_id,
            base_ref=args.base,
            head_ref=args.head,
            expected_revision=args.expect_revision,
            advisory_dirty=args.advisory_dirty,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "remediation-start":
        return remediation_start(
            root,
            change_id=args.change_id,
            dry_run=args.dry_run,
        )
    if command == "remediation-recover":
        return remediation_recover(
            root,
            change_id=args.change_id,
            review_id=args.review_id,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "candidate-ready":
        return candidate_ready(
            root,
            change_id=args.change_id,
            base_ref=args.base,
            addressed=_split_values(args.address),
            noted=_split_values(args.note),
            extra_commands=_split_values(args.extra_command),
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "candidate-status":
        return candidate_status(
            root,
            change_id=args.change_id,
            operation_id=args.operation_id,
        )
    if command == "review-ready":
        return review_ready(
            root,
            change_id=args.change_id,
            base_ref=args.base,
            expected_revision=args.expect_revision,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "review-start":
        return review_start(
            root,
            change_id=args.change_id,
            pack_path=args.pack,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "review-run":
        return review_run(
            root,
            change_id=args.change_id,
            pack_path=args.pack,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "review-status":
        return review_status(
            root,
            change_id=args.change_id,
            review_id=args.review_id,
            verbose=args.verbose,
        )
    if command == "review-import":
        return review_import(
            root,
            change_id=args.change_id,
            report_path=args.report_path,
            expected_revision=args.expect_revision,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "finding" and args.finding_command == "set":
        return finding_disposition(
            root,
            change_id=args.change_id,
            finding_id=args.finding_id,
            disposition_status=args.disposition_status,
            rationale=args.rationale,
            expected_revision=args.expect_revision,
            git_sha=args.git_sha,
            evidence=_evidence_paths(args.evidence),
            actor=args.actor,
            prompt=args.prompt,
            response=args.response,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    if command == "validate":
        return validate_command(
            root,
            change_id=args.change_id,
            command_id=args.command_id,
            expected_revision=args.expect_revision,
            operation_id=args.operation_id,
            dry_run=args.dry_run,
        )
    raise AssertionError(f"Unhandled command: {command}")


def _human_result(args: argparse.Namespace, result: dict[str, Any]) -> str:
    prefix = "DRY RUN — " if result.get("dry_run") else ""
    command = args.command
    if command == "init":
        return prefix + "DLS repository layout ready: " + ", ".join(result["actions"])
    if command == "doctor":
        failed = [item["id"] for item in result["checks"] if not item["ok"]]
        conflicts = result["global_conflicts"]
        return (
            f"DLS {result['version']}: {'ready' if result['ok'] else 'not ready'}; "
            f"failed={','.join(failed) or 'none'}; "
            f"legacy_plugins={len(conflicts['enabled_legacy_process_plugins'])}; "
            f"custom_agents={conflicts['custom_agent_count']}"
        )
    if command == "new":
        return prefix + (
            f"{result['change_id']}: {result.get('path', 'package')} — "
            f"{', '.join(result['artifacts']) or 'no artifacts'}"
        )
    if command == "adopt":
        return prefix + (
            f"{result['change_id']}: adopted {len(result['artifacts'])} artifacts, "
            f"{len(result['tickets'])} tickets"
        )
    if command == "worktree":
        if args.worktree_command == "list":
            valid = sum(1 for item in result["worktrees"] if item["valid"])
            return (
                f"worktrees={len(result['worktrees'])}; valid={valid}; "
                f"registry={result['registry_path']}"
            )
        if args.worktree_command == "register":
            return prefix + (
                f"{result['change_id']} -> {result['worktree']['owner_root']}; "
                f"{'registered' if result['changed'] else 'unchanged'}"
            )
        if args.worktree_command == "verify":
            return (
                f"{result['change_id']} -> {result['worktree']['owner_root']}; valid"
            )
        return prefix + (
            f"{result['change_id']}: "
            f"{'unregistered' if result['changed'] else 'not registered'}"
        )
    if command == "status":
        approval_summary = ",".join(
            f"{item['decision']}:{item['status']}" for item in result["approvals"]
        ) or "none"
        return (
            f"{result['change_id']} r{result['state_revision']} "
            f"{result['phase']}/{result['lifecycle']}; approvals={approval_summary}; "
            f"dirty={len(result['source_dirty_paths'])}; digest={result['definition_digest'][:8]}"
        )
    if command == "check":
        failed = [item["id"] for item in result["checks"] if not item["ok"]]
        return (
            f"{result['change_id']} {result['gate']}: "
            f"{'PASS' if result['ok'] else 'FAIL'}"
            + (f" — {', '.join(failed)}" if failed else "")
        )
    if command == "context":
        totals = result["manifest"]["totals"]
        return prefix + (
            f"context {result['phase']} {result['manifest']['manifest_digest'][:12]}; "
            f"inputs={len(result['manifest']['inputs'])}; "
            f"tokens≈{totals['estimated_tokens_low']}-{totals['estimated_tokens_high']}; "
            f"path={result['manifest_path'] or 'not written'}"
        )
    if command == "approve":
        return prefix + (
            f"{result['change_id']} r{result['state_revision']}: "
            f"{result['approval']['decision']} {result['approval']['object_digest'][:8]}"
        )
    if command == "ticket":
        return prefix + (
            f"{result['change_id']} r{result['state_revision']}: "
            f"{result['ticket_id']}={result['ticket']['status']}"
        )
    if command == "evidence":
        return prefix + (
            f"{result['change_id']} r{result['state_revision']}: "
            f"evidence={result.get('evidence_path') or 'not written'} "
            f"exit={result['evidence']['exit_code']}"
        )
    if command == "review-pack":
        return prefix + (
            f"review {result['review_id']} {result['review_pack']['mode']}; "
            f"head={result['review_pack']['head_sha'][:12]}; "
            f"path={result.get('review_pack_path') or 'not written'}"
        )
    if command == "remediation-start":
        if not result["ok"]:
            next_action = result["next_action"]
            return prefix + (
                f"remediation unavailable; next={next_action['id']}; "
                f"{next_action['detail']}"
            )
        if result["remediation_manifest"] is None:
            return prefix + (
                f"remediation {result['review_id']}; "
                f"next={result['next_action']['id']}"
            )
        return prefix + (
            f"remediation {result['review_id']}; "
            f"findings={len(result['remediation_manifest']['open_findings'])}; "
            f"path={result.get('remediation_manifest_path') or 'not written'}"
        )
    if command == "remediation-recover":
        manifest = result.get("remediation_manifest")
        return prefix + (
            f"remediation recovery {result['review_id']}; "
            f"findings={len(manifest['open_findings']) if manifest else 0}; "
            f"path={result.get('remediation_manifest_path') or result.get('projected_remediation_manifest_path') or 'none'}; "
            f"next={result['next_action']['id']}"
        )
    if command == "candidate-ready":
        return prefix + (
            f"candidate {result.get('status')}; "
            f"phase={result.get('phase')}; "
            f"pack={result.get('review_pack_path') or 'none'}; "
            f"next={result['next_action']['id']}"
        )
    if command == "candidate-status":
        return (
            f"candidate {result.get('status')}; "
            f"phase={result.get('phase') or 'none'}; "
            f"active={result.get('active_command') or 'none'}; "
            f"completed={len(result.get('completed_commands', []))}; "
            f"remaining={len(result.get('remaining_commands', []))}; "
            f"next={result['next_action']['id']}"
        )
    if command == "review-ready":
        next_action = result["next_action"]
        if not result["ok"]:
            return prefix + (
                f"review not ready; next={next_action['id']}; "
                f"{next_action['detail']}"
            )
        return prefix + (
            f"review {result['review_id']} "
            f"{result['review_pack']['review_mode']}; "
            f"next={next_action['id']}; "
            f"path={result.get('review_pack_path') or 'not written'}"
        )
    if command == "review-start":
        if not result["ok"]:
            next_action = result["next_action"]
            return prefix + (
                f"review not started; next={next_action['id']}; "
                f"{next_action['detail']}"
            )
        native = result.get("native")
        native_status = native.get("status") if isinstance(native, dict) else "not-required"
        pack_status = "created" if result.get("pack_created") else "reused"
        return prefix + (
            f"review-start {result['review_id']}; pack={pack_status}; "
            f"native={native_status}; "
            f"semantic={result['semantic_model']}/{result['semantic_reasoning_effort']}; "
            f"context={result.get('review_context_path') or 'not written'}"
        )
    if command == "review-run":
        if not result["ok"]:
            next_action = result.get("next_action", {})
            return prefix + (
                f"review-run {result.get('status', 'blocked')}; "
                f"next={next_action.get('id', 'inspect')}; "
                f"{next_action.get('detail', '')}"
            )
        return prefix + (
            f"review-run {result.get('status')}; "
            f"verdict={result.get('verdict') or 'pending'}; "
            f"result={result.get('review_result_path') or 'pending'}"
        )
    if command == "review-status":
        progress = result.get("progress", {})
        return (
            f"review {result.get('review_id') or 'none'} "
            f"{result['status']}; "
            f"stage={progress.get('stage') or 'none'}; "
            f"lanes={progress.get('completed_lanes', 0)}/"
            f"{progress.get('projected_lanes', 0)}; "
            f"verdict={result.get('verdict') or 'pending'}; "
            f"result={result.get('review_result_path') or 'none'}; "
            f"next={result['next_action']['id']}"
        )
    if command == "review-import":
        return prefix + (
            f"review {result['verdict']}; findings="
            + ",".join(f"{key}:{value}" for key, value in result["finding_counts"].items())
            + f"; remediation={result.get('remediation_manifest_path') or 'none'}"
        )
    if command == "finding":
        disposition = result["disposition"]
        return prefix + (
            f"{disposition['finding_id']}={disposition['status']}; "
            f"evidence={len(disposition['evidence'])}"
        )
    if command == "validate":
        validation = result["validation"]
        if result.get("dry_run"):
            return prefix + (
                f"{validation['command_id']}: argv={validation['argv']}; "
                f"cwd={validation['cwd']}; timeout={validation['timeout_seconds']}s"
            )
        return (
            f"{result['change_id']} validation: {'PASS' if result['ok'] else 'FAIL'}; "
            f"timeout={validation.get('timed_out', False)}; "
            f"overflow={validation.get('output_overflow', False)}"
        )
    return json.dumps(result, sort_keys=True, ensure_ascii=False)


def _revision(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expect-revision", required=True, type=int)


def _operation_id(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--operation-id")


def _dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")


def _split_values(values: list[str]) -> list[str]:
    output: list[str] = []
    for value in values:
        output.extend(item.strip() for item in value.split(",") if item.strip())
    return output


def _evidence_paths(values: list[str]) -> list[str]:
    """Normalize every supported CLI spelling to one stable path list."""
    output: list[str] = []
    seen: set[str] = set()
    for value in _split_values(values):
        if value in seen:
            continue
        seen.add(value)
        output.append(value)
    return output


def _key_value_pairs(values: list[str], option_name: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        key = key.strip()
        item = item.strip()
        if not separator or not key or not item:
            raise UsageError(f"{option_name} must use KEY=VALUE")
        if key in output:
            raise UsageError(f"Duplicate {option_name} key: {key}")
        output[key] = item
    return output


if __name__ == "__main__":
    raise SystemExit(main())
