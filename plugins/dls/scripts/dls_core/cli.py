"""Compact public CLI for DLS v0.11."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import VERSION
from .core import (
    adopt_change,
    approve,
    create_change,
    dependency_remove,
    dependency_set,
    doctor,
    init_repository,
    load_state,
    status,
    ticket_set,
    upgrade,
)
from .errors import ConfigError, DLSError, IntegrityError, UsageError
from .repo import find_repo_root
from .runner import candidate_ready, review_run
from .worktrees import prepare, resolve_change_root


def _dry_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dls", description="Human-controlled AI delivery.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init")
    _dry_run(init)
    commands.add_parser("doctor")

    new = commands.add_parser("new")
    new.add_argument("change_id")
    new.add_argument("--slug", required=True)
    new.add_argument("--title", required=True)
    new.add_argument("--kind", required=True, choices=("feature", "bug", "chore", "spike", "hotfix"))
    new.add_argument("--control", required=True, choices=("micro", "routine", "standard", "critical"))
    new.add_argument("--impact", action="append", default=[])
    new.add_argument("--outcome", required=True)
    new.add_argument("--with-tickets", action="store_true")
    new.add_argument("--with-adr", action="store_true")
    _dry_run(new)

    adopt = commands.add_parser("adopt")
    adopt.add_argument("change_id")
    adopt.add_argument("--slug", required=True)
    adopt.add_argument("--kind", required=True, choices=("feature", "bug", "chore", "spike", "hotfix"))
    adopt.add_argument("--control", required=True, choices=("micro", "routine", "standard", "critical"))
    adopt.add_argument("--impact", action="append", default=[])
    adopt.add_argument("--artifact", action="append", default=[], metavar="KEY=PATH")
    adopt.add_argument("--ticket-status", action="append", default=[], metavar="ID=STATUS")
    adopt.add_argument("--requirement-prefix", action="append", default=[])
    _dry_run(adopt)

    upgrade_parser = commands.add_parser("upgrade")
    mode = upgrade_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")

    status_parser = commands.add_parser("status")
    status_parser.add_argument("change_id")
    status_parser.add_argument("--details", choices=("findings", "receipt", "metrics", "history"))

    approve_parser = commands.add_parser("approve")
    approve_parser.add_argument("change_id")
    approve_parser.add_argument("--decision", required=True, choices=("definition", "architecture", "design", "accept"))
    approve_parser.add_argument("--include-design", action="store_true")
    approve_parser.add_argument("--include-architecture", action="store_true")
    approve_parser.add_argument("--actor", default="user", choices=("user",))
    approve_parser.add_argument("--response", required=True)
    approve_parser.add_argument("--git-sha")
    approve_parser.add_argument("--decision-id")
    _dry_run(approve_parser)

    ticket = commands.add_parser("ticket")
    ticket.add_argument("change_id")
    ticket.add_argument("ticket_id")
    ticket.add_argument("--status", required=True)
    ticket.add_argument("--note")

    dependency = commands.add_parser("dependency")
    dependency_commands = dependency.add_subparsers(dest="dependency_command", required=True)
    dep_set = dependency_commands.add_parser("set")
    dep_set.add_argument("change_id")
    dep_set.add_argument("--on", required=True)
    _dry_run(dep_set)
    dep_list = dependency_commands.add_parser("list")
    dep_list.add_argument("change_id")
    dep_remove = dependency_commands.add_parser("remove")
    dep_remove.add_argument("change_id")
    dep_remove.add_argument("--on", required=True)
    _dry_run(dep_remove)

    candidate = commands.add_parser("candidate-ready")
    candidate.add_argument("change_id")
    candidate.add_argument("--base")
    candidate.add_argument("--address", action="append", default=[])
    candidate.add_argument("--note", action="append", default=[])
    _dry_run(candidate)

    review = commands.add_parser("review-run")
    review.add_argument("change_id")
    review.add_argument("--kind", required=True, choices=("definition", "code"))
    review.add_argument("--stream", action="store_true")
    _dry_run(review)

    worktree = commands.add_parser("worktree")
    worktree_commands = worktree.add_subparsers(dest="worktree_command", required=True)
    worktree_prepare = worktree_commands.add_parser("prepare")
    worktree_prepare.add_argument("change_id")
    worktree_prepare.add_argument("--base", required=True)
    worktree_prepare.add_argument("--path", type=Path)
    worktree_prepare.add_argument("--branch")
    _dry_run(worktree_prepare)
    return parser


def _pairs(values: list[str], *, label: str) -> dict[str, str]:
    output: dict[str, str] = {}
    for value in values:
        key, separator, item = value.partition("=")
        if not separator or not key or not item or key in output:
            raise UsageError(f"{label} must use unique KEY=VALUE entries")
        output[key] = item
    return output


def _owner(root: Path, change_id: str) -> Path:
    return resolve_change_root(root, change_id)


def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    root = find_repo_root(args.root or Path.cwd())
    if args.command == "init":
        return init_repository(root, dry_run=args.dry_run)
    if args.command == "doctor":
        return doctor(root)
    if args.command == "upgrade":
        return upgrade(root, apply=args.apply)
    if args.command == "new":
        return create_change(
            root,
            change_id=args.change_id,
            slug=args.slug,
            title=args.title,
            kind=args.kind,
            control=args.control,
            impact_tags=args.impact,
            outcome=args.outcome,
            with_tickets=args.with_tickets,
            with_adr=args.with_adr,
            dry_run=args.dry_run,
        )
    if args.command == "adopt":
        artifacts = {
            key: {"path": value}
            for key, value in _pairs(args.artifact, label="artifact").items()
        }
        tickets = {
            key: {"status": value, "note": None, "updated_at": None}
            for key, value in _pairs(args.ticket_status, label="ticket-status").items()
        }
        return adopt_change(
            root,
            change_id=args.change_id,
            slug=args.slug,
            kind=args.kind,
            control=args.control,
            impact_tags=args.impact,
            artifacts=artifacts,
            tickets=tickets,
            requirement_prefixes=args.requirement_prefix,
            dry_run=args.dry_run,
        )
    if args.command == "worktree":
        return prepare(
            root,
            change_id=args.change_id,
            base=args.base,
            path=args.path,
            branch=args.branch,
            dry_run=args.dry_run,
        )
    owner = _owner(root, args.change_id)
    if args.command == "status":
        return status(owner, args.change_id, details=args.details)
    if args.command == "approve":
        return approve(
            owner,
            change_id=args.change_id,
            decision=args.decision,
            include_design=args.include_design,
            include_architecture=args.include_architecture,
            actor=args.actor,
            response=args.response,
            git_sha=args.git_sha,
            dry_run=args.dry_run,
            decision_id=args.decision_id,
        )
    if args.command == "ticket":
        return ticket_set(
            owner,
            change_id=args.change_id,
            ticket_id=args.ticket_id,
            value=args.status,
            note=args.note,
        )
    if args.command == "dependency":
        if args.dependency_command == "list":
            state = load_state(owner, args.change_id)
            return {"ok": True, "change_id": args.change_id, "dependencies": state["dependencies"]}
        if args.dependency_command == "set":
            return dependency_set(
                owner,
                change_id=args.change_id,
                target=args.on,
                dry_run=args.dry_run,
            )
        return dependency_remove(
            owner,
            change_id=args.change_id,
            target=args.on,
            dry_run=args.dry_run,
        )
    if args.command == "candidate-ready":
        return candidate_ready(
            owner,
            change_id=args.change_id,
            base=args.base,
            addressed=args.address,
            noted=args.note,
            dry_run=args.dry_run,
        )
    if args.command == "review-run":
        callback = None
        if args.stream:
            callback = lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)
        return review_run(
            owner,
            change_id=args.change_id,
            kind=args.kind,
            stream=callback,
            dry_run=args.dry_run,
        )
    raise UsageError(f"Unsupported command: {args.command}")


def _human(value: dict[str, Any]) -> str:
    if value.get("status") == "completed" and value.get("review_result_path"):
        return (
            f"Review {value.get('verdict')}: {value['review_result_path']}\n"
            f"Next: {(value.get('next_action') or {}).get('id')}"
        )
    return json.dumps(value, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
        if not (args.command == "review-run" and args.stream):
            print(json.dumps(result, ensure_ascii=False) if args.as_json else _human(result))
        return 0
    except UsageError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "kind": "usage"}), file=sys.stderr)
        return 2
    except (IntegrityError, ConfigError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "kind": "integrity"}), file=sys.stderr)
        return 3
    except DLSError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "kind": "dls"}), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
