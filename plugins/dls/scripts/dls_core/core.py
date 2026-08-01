"""Small current-only DLS lifecycle kernel.

The v0.11 contract deliberately stores current delivery truth once. Historical
v0.10 artifacts remain readable files, but ordinary runtime never executes
their recovery contracts.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from .errors import ConfigError, IntegrityError, UsageError
from .io import (
    FileLock,
    atomic_write_json,
    atomic_write_text,
    canonical_text,
    read_json,
    safe_resolve,
    sha256_bytes,
    sha256_file,
    utc_now,
)
from .repo import (
    TEMPLATES_ROOT,
    command_contract_digest,
    copy_asset,
    git_head,
    git_product_tree_digest,
    git_source_dirty_paths,
    load_config,
    package_digest,
    package_digest_at_revision,
    resolve_profile,
    render_template,
    run_git,
)

STATE_SCHEMA = 2
PACK_SCHEMA = 3
RESULT_SCHEMA = 3
CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
CONTROLS = {"micro", "routine", "standard", "critical"}
KINDS = {"feature", "bug", "chore", "spike", "hotfix"}
TICKET_STATES = {"planned", "blocked", "in-progress", "implemented", "validated", "done"}
DECISIONS = {"definition", "architecture", "design", "accept"}
ARCHITECTURE_DIGEST_CONTRACT = "dls-architecture-digest/v1"
DESIGN_DIGEST_CONTRACT = "dls-design-digest/v1"
DEFINITION_DIGEST_REBASE_CONTRACT = "dls-definition-digest-rebase/v1"
ARCH_START = "<!-- dls:architecture:start -->"
ARCH_END = "<!-- dls:architecture:end -->"
DESIGN_START = "<!-- dls:design:start -->"
DESIGN_END = "<!-- dls:design:end -->"
UI_HEADINGS = {"ui/ux source", "ui/ux contract"}


def stable_digest(value: object) -> str:
    return sha256_bytes(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def validate_change_id(value: str) -> str:
    if not CHANGE_ID_RE.fullmatch(value):
        raise UsageError("Invalid change ID")
    return value


def state_path(root: Path, change_id: str) -> Path:
    return root / ".dls" / "state" / f"{validate_change_id(change_id)}.json"


def _validate_artifacts(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        raise IntegrityError("State requires authored artifacts")
    output: dict[str, dict[str, Any]] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            raise IntegrityError("Artifact entries must be objects")
        path = item.get("path")
        if not isinstance(path, str) or not path or Path(path).is_absolute():
            raise IntegrityError(f"Invalid artifact path: {name}")
        normalized_name = name.lower().replace("_", "-")
        filename = Path(path).name.lower()
        inferred_role = (
            "execution"
            if normalized_name in {"changelog", "evidence", "release-evidence", "validation"}
            or "changelog" in normalized_name
            or filename.startswith("changelog")
            else "definition"
        )
        output[name] = {
            "path": path,
            "role": (
                item["role"]
                if item.get("role") in {"definition", "execution"}
                else inferred_role
            ),
        }
        scope = item.get("producer_ticket_scope")
        if isinstance(scope, list):
            output[name]["producer_ticket_scope"] = [
                item for item in scope if isinstance(item, str) and item
            ]
    return output


def validate_state(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != STATE_SCHEMA:
        raise IntegrityError("DLS state requires schema_version 2; run `dls upgrade --dry-run`")
    if not isinstance(value.get("revision"), int) or value["revision"] < 1:
        raise IntegrityError("State revision must be positive")
    change = value.get("change")
    if not isinstance(change, dict):
        raise IntegrityError("State change metadata is missing")
    validate_change_id(str(change.get("id") or ""))
    if change.get("kind") not in KINDS or change.get("control") not in CONTROLS:
        raise IntegrityError("Invalid change kind or control")
    tags = change.get("impact_tags")
    if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
        raise IntegrityError("impact_tags must be strings")
    change["artifacts"] = _validate_artifacts(change.get("artifacts"))
    for key, expected in (
        ("approvals", list),
        ("tickets", dict),
        ("dependencies", list),
        ("findings", dict),
    ):
        if not isinstance(value.get(key), expected):
            raise IntegrityError(f"state.{key} must be {expected.__name__}")
    for dependency in value["dependencies"]:
        if (
            not isinstance(dependency, dict)
            or dependency.get("requires") != "accepted-in-base"
            or not isinstance(dependency.get("change_id"), str)
        ):
            raise IntegrityError("Only accepted-in-base implementation dependencies are supported")
    for ticket_id, ticket in value["tickets"].items():
        if not isinstance(ticket_id, str) or not isinstance(ticket, dict):
            raise IntegrityError("Invalid ticket state")
        if ticket.get("status") not in TICKET_STATES:
            raise IntegrityError(f"Invalid ticket status: {ticket_id}")
    return value


def load_state(root: Path, change_id: str, *, allow_legacy: bool = False) -> dict[str, Any]:
    if (root / ".dls" / "upgrade-incomplete").exists():
        raise IntegrityError("DLS upgrade is incomplete; rerun `dls upgrade --apply`")
    if not allow_legacy:
        schemas = {
            read_json(path).get("schema_version")
            for path in (root / ".dls" / "state").glob("*.json")
        }
        if schemas != {STATE_SCHEMA}:
            message = (
                "DLS upgrade is incomplete"
                if STATE_SCHEMA in schemas
                else "DLS state requires upgrade"
            )
            raise IntegrityError(f"{message}; run `dls upgrade --dry-run`")
    value = read_json(state_path(root, change_id))
    if value.get("schema_version") == 1 and allow_legacy:
        return value
    return validate_state(value)


def write_state(root: Path, value: dict[str, Any]) -> None:
    validate_state(value)
    atomic_write_json(state_path(root, value["change"]["id"]), value, backup=False)


def mutate_state(
    root: Path,
    change_id: str,
    callback: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    path = state_path(root, change_id)
    with FileLock(path.with_suffix(".lock")):
        current = load_state(root, change_id)
        updated = copy.deepcopy(current)
        callback(updated)
        updated["revision"] = current["revision"] + 1
        write_state(root, updated)
        return updated


def authored_artifacts(state: dict[str, Any]) -> dict[str, Any]:
    return {
        name: item
        for name, item in state["change"]["artifacts"].items()
        if item.get("role") != "execution"
    }


def definition_digest(root: Path, state: dict[str, Any], revision: str | None = None) -> str:
    artifacts = authored_artifacts(state)
    if revision is None:
        return package_digest(root, artifacts)
    digest = package_digest_at_revision(root, artifacts, revision)
    if digest is None:
        raise IntegrityError(f"Definition artifacts are missing at {revision}")
    return digest


def _section(text: str, headings: set[str]) -> str | None:
    lines = canonical_text(text).splitlines()
    start: int | None = None
    level = 0
    output: list[str] = []
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if start is None:
            if match and match.group(2).strip().lower() in headings:
                start = index
                level = len(match.group(1))
                output.append(line)
        elif match and len(match.group(1)) <= level:
            break
        elif start is not None:
            output.append(line)
    text_value = "\n".join(output).strip()
    return text_value or None


def _stable_contract_digest(contract: str, payload: dict[str, Any]) -> str:
    return stable_digest({"contract": contract, **payload})


def _architecture_source(root: Path, state: dict[str, Any]) -> dict[str, str] | None:
    artifacts = state["change"]["artifacts"]
    for role in ("adr", "spec"):
        item = artifacts.get(role)
        if not isinstance(item, dict):
            continue
        path = safe_resolve(root, item["path"], must_exist=True)
        text = canonical_text(path.read_text(encoding="utf-8"))
        if role == "adr":
            body = text.strip()
            if body:
                return {"source_kind": "adr", "content": body}
            continue
        if ARCH_START in text or ARCH_END in text:
            if text.count(ARCH_START) != 1 or text.count(ARCH_END) != 1:
                raise IntegrityError("Architecture decision region is ambiguous")
            body = text.split(ARCH_START, 1)[1].split(ARCH_END, 1)[0].strip()
            if not body:
                raise IntegrityError("Architecture decision region is empty")
            return {"source_kind": "spec-marker", "content": body}
        legacy = _section(text, {"architecture", "architecture and alternatives"})
        if legacy:
            return {"source_kind": "spec-heading", "content": legacy}
    return None


def _design_source(root: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    value: dict[str, Any] | None = None
    for role in ("spec", "change"):
        item = state["change"]["artifacts"].get(role)
        if not isinstance(item, dict):
            continue
        text = safe_resolve(root, item["path"], must_exist=True).read_text(encoding="utf-8")
        text = canonical_text(text)
        if DESIGN_START in text or DESIGN_END in text:
            if text.count(DESIGN_START) != 1 or text.count(DESIGN_END) != 1:
                raise IntegrityError("Design decision region is ambiguous")
            section = text.split(DESIGN_START, 1)[1].split(DESIGN_END, 1)[0].strip()
        else:
            section = _section(text, UI_HEADINGS)
        if not section:
            continue
        fields: dict[str, str] = {}
        for line in section.splitlines():
            match = re.fullmatch(
                r"\s*(Mode|Kind|Reference|Version|Rationale)\s*:\s*(.+?)\s*",
                line,
                re.IGNORECASE,
            )
            if match:
                fields[match.group(1).lower()] = match.group(2)
        if fields:
            value = {
                "mode": fields.get("mode"),
                "kind": fields.get("kind"),
                "ref": fields.get("reference"),
                "version": fields.get("version"),
                "rationale": fields.get("rationale"),
            }
            break
    if value is None:
        legacy = state["change"].get("design")
        value = legacy if isinstance(legacy, dict) else None
    if not isinstance(value, dict):
        return None
    mode = value.get("mode")
    if mode == "bypass":
        rationale = value.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            raise IntegrityError("Design bypass requires a rationale")
        return {"mode": "bypass", "rationale": rationale.strip()}
    if mode != "source" or value.get("kind") not in {
        "precedent",
        "artifact",
        "external-version",
    }:
        raise IntegrityError("Design source must be precedent, artifact, external-version, or bypass")
    reference = value.get("ref")
    version = value.get("version")
    if not isinstance(reference, str) or not reference:
        raise IntegrityError("Design source reference is missing")
    if value["kind"] == "external-version":
        if not reference.startswith("https://") or not isinstance(version, str) or not version:
            raise IntegrityError("External design source requires HTTPS and an immutable version")
        return {"mode": mode, "kind": value["kind"], "ref": reference, "version": version}
    candidate = Path(reference)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise IntegrityError("Repository design source path is unsafe")
    path = safe_resolve(root, reference, must_exist=True)
    blob = run_git(root, "rev-parse", f"HEAD:{reference}", check=False).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", blob):
        raise IntegrityError("Repository design source must be committed")
    if version not in {None, f"git:{blob}"}:
        raise IntegrityError("Repository design source version changed")
    return {
        "mode": mode,
        "kind": value["kind"],
        "ref": reference,
        "version": f"git:{blob}",
        "content_digest": sha256_file(path),
    }


def decision_projection(root: Path, state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    definition = definition_digest(root, state)
    architecture = _architecture_source(root, state)
    design = _design_source(root, state)
    tags = set(state["change"]["impact_tags"])
    output = {
        "definition": {"required": True, "digest": definition},
        "architecture": {
            "required": "architecture" in tags or "adr" in state["change"]["artifacts"],
            "digest": (
                _stable_contract_digest(
                    ARCHITECTURE_DIGEST_CONTRACT,
                    {
                        "source_kind": architecture["source_kind"],
                        "content": architecture["content"],
                    },
                )
                if architecture
                else None
            ),
        },
        "design": {
            "required": "user-interface" in tags,
            "digest": (
                _stable_contract_digest(DESIGN_DIGEST_CONTRACT, {"design": design})
                if design
                else None
            ),
        },
    }
    for name, item in output.items():
        if item["required"] and not item["digest"]:
            raise IntegrityError(f"Required {name} decision source is missing")
    return output


def current_approval(state: dict[str, Any], decision: str, digest: str | None) -> dict[str, Any] | None:
    for item in reversed(state["approvals"]):
        if (
            item.get("decision") == decision
            and item.get("digest") == digest
            and item.get("status") == "current"
        ):
            return item
    return None


def definition_review_current(
    root: Path,
    state: dict[str, Any],
    projection: dict[str, dict[str, Any]] | None = None,
) -> bool:
    if state["change"]["control"] in {"micro", "routine"}:
        return True
    review = state.get("definition_review")
    if not isinstance(review, dict) or review.get("verdict") != "review-clear":
        return False
    profile_digest = review.get("profile_digest")
    if (
        profile_digest is not None
        and profile_digest != resolve_profile(root)["digest"]
    ):
        return False
    decisions = projection or decision_projection(root, state)
    return (
        review.get("definition_digest") == decisions["definition"]["digest"]
        and review.get("decision_digests")
        == {
            key: item["digest"]
            for key, item in decisions.items()
            if item["required"] and key != "definition"
        }
    )


def pending_decisions(root: Path, state: dict[str, Any]) -> list[dict[str, str]]:
    projection = decision_projection(root, state)
    if not definition_review_current(root, state, projection):
        return []
    pending: list[dict[str, str]] = []
    for decision in ("definition", "design", "architecture"):
        item = projection[decision]
        digest = item["digest"]
        if item["required"] and current_approval(state, decision, digest) is None:
            assert isinstance(digest, str)
            pending.append({"decision": decision, "digest": digest})
    return pending


def decision_action(items: list[dict[str, str]]) -> dict[str, Any] | None:
    if not items:
        return None
    names = [item["decision"] for item in items]
    identifier = "approve-" + "-and-".join(names)
    return {
        "id": identifier,
        "approvals": items,
        "detail": "; ".join(f"{item['decision']}={item['digest'][:12]}" for item in items),
    }


def accepted_record(state: dict[str, Any]) -> dict[str, Any] | None:
    for item in reversed(state["approvals"]):
        if item.get("decision") == "accept" and item.get("status") == "current":
            return item
    return None


def _product_revision_current(root: Path, revision: object) -> bool:
    if not isinstance(revision, str):
        return False
    current = git_product_tree_digest(root)
    reviewed = git_product_tree_digest(root, revision)
    return current is not None and reviewed == current


def current_review(root: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    review = state.get("review")
    if not isinstance(review, dict):
        return None
    if (
        not _product_revision_current(root, review.get("head_sha"))
        or review.get("definition_digest") != definition_digest(root, state)
        or git_source_dirty_paths(root)
    ):
        return None
    profile_digest = review.get("profile_digest")
    if profile_digest is not None and profile_digest != resolve_profile(root)["digest"]:
        return None
    source_digest = review.get("source_digest")
    if source_digest is not None and source_digest != git_product_tree_digest(root):
        return None
    return review


def dependency_status(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    from .worktrees import git_common_dir, resolve_change_root

    head = git_head(root)
    blocked: list[dict[str, Any]] = []
    for item in state["dependencies"]:
        target_root = resolve_change_root(root, item["change_id"])
        if git_common_dir(target_root) != git_common_dir(root):
            raise IntegrityError("Cross-repository dependencies are not supported")
        target = load_state(target_root, item["change_id"])
        if item.get("target_definition_digest") != definition_digest(target_root, target):
            blocked.append({**item, "reason": "target-definition-changed"})
            continue
        accepted = accepted_record(target)
        if accepted is None:
            blocked.append({**item, "reason": "not-accepted"})
            continue
        target_head = accepted.get("git_sha")
        if not isinstance(target_head, str):
            blocked.append({**item, "reason": "accepted-head-missing"})
            continue
        ancestry = run_git(root, "merge-base", "--is-ancestor", target_head, head or "", check=False)
        if ancestry.returncode != 0:
            blocked.append({**item, "reason": "accepted-head-not-in-base", "accepted_head": target_head})
    return {"satisfied": not blocked, "blocked": blocked}


def next_action(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    review = current_review(root, state)
    accepted = accepted_record(state)
    if (
        accepted
        and isinstance(review, dict)
        and accepted.get("git_sha") == review.get("head_sha")
        and review.get("verdict") == "review-clear"
    ):
        return {"id": "accepted"}
    if not definition_review_current(root, state):
        return {"id": "run-definition-review"}
    pending = pending_decisions(root, state)
    if pending:
        return decision_action(pending) or {"id": "approve-definition"}
    dependency = dependency_status(root, state)
    if dependency["blocked"]:
        reason = dependency["blocked"][0]["reason"]
        return {
            "id": "rebase-after-dependency" if reason == "accepted-head-not-in-base" else "wait-dependency",
            "detail": dependency["blocked"],
        }
    head = git_head(root)
    candidate = current_candidate(root, state)
    if isinstance(review, dict) and review.get("head_sha") == head:
        if review.get("verdict") == "review-clear":
            return {"id": "accept"}
        return {"id": "remediate-findings"}
    if isinstance(candidate, dict) and candidate.get("head_sha") == head and candidate.get("status") == "ready":
        return {"id": "open-review-task"}
    if (
        not git_source_dirty_paths(root)
        and state["tickets"]
        and all(
            item["status"] in {"implemented", "validated", "done"}
            for item in state["tickets"].values()
        )
    ):
        return {"id": "run-candidate-ready"}
    return {"id": "continue-implementation"}


def current_candidate(root: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    candidate = state.get("candidate")
    if not isinstance(candidate, dict) or candidate.get("status") != "ready":
        return None
    config = load_config(root)
    commands = list(config.get("policy", {}).get("review_required_commands", []))
    policy_digest = stable_digest(
        {command: command_contract_digest(root, command) for command in commands}
    )
    profile_digest = resolve_profile(root, config=config)["digest"]
    if (
        candidate.get("head_sha") != git_head(root)
        or candidate.get("source_digest") != git_product_tree_digest(root)
        or candidate.get("definition_digest") != definition_digest(root, state)
        or candidate.get("policy_digest") != policy_digest
        or candidate.get("profile_digest") != profile_digest
    ):
        return None
    relative = candidate.get("pack_path")
    if not isinstance(relative, str):
        return None
    path = safe_resolve(root, relative)
    if not path.is_file():
        return None
    pack = read_json(path)
    digest = stable_digest({key: value for key, value in pack.items() if key != "pack_digest"})
    if (
        pack.get("schema_version") != PACK_SCHEMA
        or pack.get("contract") != "dls-review-pack/v3"
        or pack.get("head_sha") != candidate["head_sha"]
        or pack.get("pack_digest") != digest
        or candidate.get("pack_digest") != digest
        or (pack.get("platform_profile") or {}).get("digest") != profile_digest
        or pack.get("validation_policy_digest") != policy_digest
    ):
        raise IntegrityError("Current ReviewPack failed integrity validation")
    return candidate


def status(root: Path, change_id: str, *, details: str | None = None) -> dict[str, Any]:
    state = load_state(root, change_id)
    projection = decision_projection(root, state)
    head = git_head(root)
    action = next_action(root, state)
    candidate = current_candidate(root, state)
    review = current_review(root, state)
    output: dict[str, Any] = {
        "ok": True,
        "schema_version": STATE_SCHEMA,
        "change_id": change_id,
        "state_revision": state["revision"],
        "head_sha": head,
        "control_level": state["change"]["control"],
        "phase": state["phase"],
        "lifecycle": state["lifecycle"],
        "source_clean": not git_source_dirty_paths(root),
        "definition_digest": projection["definition"]["digest"],
        "decisions": projection,
        "candidate_head": (candidate or {}).get("head_sha"),
        "review_id": (review or {}).get("review_id"),
        "review_head": (review or {}).get("head_sha"),
        "review_verdict": (review or {}).get("verdict"),
        "next_action": action,
    }
    if details == "findings":
        output["findings"] = list(state["findings"].values())[:64]
        output["omitted_count"] = max(0, len(state["findings"]) - 64)
    elif details == "metrics":
        output["metrics"] = (review or {}).get("usage", {})
    elif details == "history":
        output["approvals"] = state["approvals"]
        output["migration"] = state.get("migration")
    elif details == "receipt":
        output["receipt"] = receipt(root, state)
    return output


def receipt(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    review = current_review(root, state) or {}
    candidate = current_candidate(root, state)
    accepted = accepted_record(state)
    accepted_current = bool(
        accepted
        and accepted.get("git_sha") == review.get("head_sha")
        and review.get("verdict") == "review-clear"
    )
    lifecycle = (
        "accepted"
        if accepted_current
        else review.get("verdict")
        or ("candidate-ready" if candidate else state["lifecycle"])
    )
    ticket_counts: dict[str, int] = {}
    for item in state["tickets"].values():
        ticket_counts[item["status"]] = ticket_counts.get(item["status"], 0) + 1
    value = {
        "change_id": state["change"]["id"],
        "head_sha": git_head(root),
        "definition_digest": definition_digest(root, state),
        "lifecycle": lifecycle,
        "tickets": ticket_counts,
        "review": {
            "id": review.get("review_id"),
            "verdict": review.get("verdict"),
            "head_sha": review.get("head_sha"),
            "finding_count": len(state["findings"]),
        },
        "accepted": accepted_current,
        "release": "not-evaluated",
        "production": "not-evaluated",
    }
    return {"digest": stable_digest(value), **value}


def initial_state(
    *,
    change_id: str,
    slug: str,
    kind: str,
    control: str,
    impact_tags: list[str],
    artifacts: dict[str, dict[str, Any]],
    tickets: dict[str, dict[str, Any]] | None = None,
    requirement_prefixes: list[str] | None = None,
) -> dict[str, Any]:
    if kind not in KINDS or control not in CONTROLS:
        raise UsageError("Invalid change kind or control")
    value = {
        "schema_version": STATE_SCHEMA,
        "revision": 1,
        "change": {
            "id": validate_change_id(change_id),
            "slug": slug,
            "kind": kind,
            "control": control,
            "impact_tags": sorted(set(impact_tags)),
            "artifacts": artifacts,
            "requirement_prefixes": sorted(set(requirement_prefixes or [])),
            "design": None,
        },
        "phase": "definition",
        "lifecycle": "draft",
        "approvals": [],
        "tickets": tickets or {},
        "dependencies": [],
        "candidate": None,
        "definition_review": None,
        "review": None,
        "findings": {},
        "acceptance": None,
        "active_run": None,
    }
    return validate_state(value)


def approve(
    root: Path,
    *,
    change_id: str,
    decision: str,
    include_design: bool,
    include_architecture: bool,
    actor: str,
    response: str,
    git_sha: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    if decision not in DECISIONS or actor != "user":
        raise UsageError("Only explicit user approvals are supported")
    state = load_state(root, change_id)
    head = git_head(root)
    if git_source_dirty_paths(root):
        raise IntegrityError("Approval requires a clean product source")
    projection = decision_projection(root, state)
    decisions = [decision]
    if decision == "definition":
        if not definition_review_current(root, state, projection):
            raise IntegrityError("Definition approval requires current semantic definition review")
        if include_design:
            decisions.append("design")
        if include_architecture:
            decisions.append("architecture")
        required = [
            name
            for name in ("design", "architecture")
            if projection[name]["required"]
            and current_approval(state, name, projection[name]["digest"]) is None
        ]
        missing = sorted(set(required) - set(decisions))
        if missing:
            raise IntegrityError("Approval bundle is incomplete: " + ", ".join(missing))
    if decision == "accept":
        review = current_review(root, state)
        if not isinstance(review, dict) or review.get("verdict") != "review-clear":
            raise IntegrityError("Acceptance requires review-clear")
        if review.get("head_sha") != git_sha or git_sha != head:
            raise IntegrityError("Acceptance must name the current reviewed HEAD")
        decisions = ["accept"]
    approval_digests: dict[str, str] = {}
    for name in decisions:
        digest = (
            projection["definition"]["digest"]
            if name == "accept"
            else projection[name]["digest"]
        )
        if not isinstance(digest, str):
            raise IntegrityError(f"{name} decision digest is unavailable")
        if digest[:12] not in response:
            raise IntegrityError(f"Response must explicitly contain {name} digest {digest[:12]}")
        approval_digests[name] = digest
    bundle_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            stable_digest(
                {
                    "contract": "dls-approval-bundle/v2",
                    "change_id": change_id,
                    "head_sha": git_sha or head,
                    "decisions": approval_digests,
                    "response": response,
                }
            ),
        )
    )
    records: list[dict[str, Any]] = []
    for name in decisions:
        digest = approval_digests[name]
        records.append(
            {
                "id": str(uuid.uuid5(uuid.UUID(bundle_id), name)),
                "bundle_id": bundle_id,
                "decision": name,
                "digest": digest,
                "git_sha": git_sha or head,
                "actor": actor,
                "response_digest": sha256_bytes(response.encode("utf-8")),
                "status": "current",
                "recorded_at": utc_now(),
            }
        )
    existing = {item["id"]: item for item in state["approvals"] if isinstance(item.get("id"), str)}
    existing_bundle = [existing[item["id"]] for item in records if item["id"] in existing]
    if existing_bundle:
        if len(existing_bundle) != len(records):
            raise IntegrityError("Approval bundle is partially recorded")
        return {
            "ok": True,
            "dry_run": dry_run,
            "state_revision": state["revision"],
            "approvals": existing_bundle,
            "next_action": next_action(root, state),
            "receipt": receipt(root, state) if decision == "accept" else None,
        }
    if dry_run:
        return {"ok": True, "dry_run": True, "approvals": records, "next_action": next_action(root, state)}

    def apply(value: dict[str, Any]) -> None:
        for record in records:
            for existing in value["approvals"]:
                if existing.get("decision") == record["decision"] and existing.get("status") == "current":
                    existing["status"] = "superseded"
                    existing["superseded_by"] = record["id"]
            value["approvals"].append(record)
        if decision == "definition":
            value["phase"] = "implementation"
            value["lifecycle"] = "approved"
        elif decision == "accept":
            value["phase"] = "accepted"
            value["lifecycle"] = "accepted"
            value["acceptance"] = records[0]["id"]

    updated = mutate_state(root, change_id, apply)
    return {
        "ok": True,
        "dry_run": False,
        "state_revision": updated["revision"],
        "approvals": records,
        "next_action": next_action(root, updated),
        "receipt": receipt(root, updated) if decision == "accept" else None,
    }


def ticket_set(root: Path, *, change_id: str, ticket_id: str, value: str, note: str | None) -> dict[str, Any]:
    if value not in TICKET_STATES:
        raise UsageError("Invalid ticket status")

    def apply(state: dict[str, Any]) -> None:
        state["tickets"][ticket_id] = {
            "status": value,
            "note": note,
            "updated_at": utc_now(),
        }

    updated = mutate_state(root, change_id, apply)
    return {"ok": True, "ticket_id": ticket_id, "status": value, "state_revision": updated["revision"]}


def _dependency_graph(root: Path, start: str, proposed: str) -> None:
    from .worktrees import resolve_change_root

    stack = [proposed]
    seen: set[str] = set()
    while stack:
        current = stack.pop()
        if current == start:
            raise IntegrityError("Dependency cycle detected")
        if current in seen:
            continue
        seen.add(current)
        if len(seen) > 64:
            raise IntegrityError("Dependency graph is too deep")
        target_root = resolve_change_root(root, current)
        target = load_state(target_root, current)
        stack.extend(item["change_id"] for item in target["dependencies"])


def dependency_set(root: Path, *, change_id: str, target: str, dry_run: bool) -> dict[str, Any]:
    from .worktrees import resolve_change_root

    validate_change_id(target)
    if target == change_id:
        raise UsageError("A change cannot depend on itself")
    target_root = resolve_change_root(root, target)
    target_state = load_state(target_root, target)
    _dependency_graph(root, change_id, target)
    record = {
        "change_id": target,
        "requires": "accepted-in-base",
        "target_definition_digest": definition_digest(target_root, target_state),
    }
    state = load_state(root, change_id)
    projected = [item for item in state["dependencies"] if item["change_id"] != target] + [record]
    if dry_run:
        return {"ok": True, "dry_run": True, "dependency": record}

    def apply(value: dict[str, Any]) -> None:
        value["dependencies"] = projected

    updated = mutate_state(root, change_id, apply)
    return {"ok": True, "dry_run": False, "dependency": record, "state_revision": updated["revision"]}


def dependency_remove(root: Path, *, change_id: str, target: str, dry_run: bool) -> dict[str, Any]:
    state = load_state(root, change_id)
    projected = [item for item in state["dependencies"] if item["change_id"] != target]
    if dry_run:
        return {"ok": True, "dry_run": True, "changed": projected != state["dependencies"]}

    def apply(value: dict[str, Any]) -> None:
        value["dependencies"] = projected

    updated = mutate_state(root, change_id, apply)
    return {"ok": True, "changed": True, "state_revision": updated["revision"]}


def _normalize_approval(item: dict[str, Any]) -> dict[str, Any] | None:
    decision = item.get("decision")
    if decision not in DECISIONS:
        return None
    digest = item.get("decision_digest") or item.get("object_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    normalized = {
        "id": str(item.get("id") or uuid.uuid4()),
        "bundle_id": item.get("approval_bundle_id"),
        "decision": decision,
        "digest": digest,
        "git_sha": item.get("git_sha"),
        "actor": "user",
        "response_digest": (
            sha256_bytes(str(item.get("response")).encode("utf-8"))
            if item.get("response") is not None
            else None
        ),
        "status": item.get("status") if item.get("status") in {"current", "superseded", "revoked"} else "current",
        "recorded_at": item.get("recorded_at") or utc_now(),
    }
    for key in ("architecture_decision_digest", "design_decision_digest"):
        if isinstance(item.get(key), str):
            normalized[key] = item[key]
    return normalized


def project_legacy_state(root: Path, legacy: dict[str, Any]) -> dict[str, Any]:
    change_id = validate_change_id(str(legacy.get("change_id") or ""))
    artifacts = _validate_artifacts(legacy.get("artifacts"))
    tickets: dict[str, dict[str, Any]] = {}
    for ticket_id, item in legacy.get("tickets", {}).items():
        if isinstance(item, dict) and item.get("status") in TICKET_STATES:
            tickets[ticket_id] = {
                "status": item["status"],
                "note": item.get("note"),
                "updated_at": item.get("updated_at"),
            }
    approvals = [
        normalized
        for item in legacy.get("approvals", [])
        if isinstance(item, dict)
        for normalized in [_normalize_approval(item)]
        if normalized is not None
    ]
    dependencies: list[dict[str, Any]] = []
    for item in legacy.get("dependencies", []):
        if not isinstance(item, dict):
            continue
        if item.get("blocks_stage") != "implementation" or item.get("requires") != "accepted-in-base":
            raise IntegrityError(f"{change_id} has unsupported legacy dependency")
        dependencies.append(
            {
                "change_id": item["change_id"],
                "requires": "accepted-in-base",
                "target_definition_digest": item.get("target_definition_digest"),
            }
        )
    latest_result = next(
        (
            item
            for item in reversed(legacy.get("reviews", []))
            if isinstance(item, dict) and item.get("kind") == "result"
        ),
        None,
    )
    review: dict[str, Any] | None = None
    findings: dict[str, Any] = {}
    if isinstance(latest_result, dict):
        result_path = latest_result.get("result_path")
        result_digest = latest_result.get("result_digest")
        if isinstance(result_path, str) and isinstance(result_digest, str):
            path = safe_resolve(root, result_path, must_exist=True)
            payload = read_json(path)
            if stable_digest(payload) != result_digest:
                raise IntegrityError(f"Legacy result digest changed: {result_path}")
            for finding in payload.get("findings", []):
                if isinstance(finding, dict) and isinstance(finding.get("id"), str):
                    findings[finding["id"]] = {
                        **finding,
                        "status": "open",
                        "review_id": latest_result.get("review_id"),
                    }
            review = {
                "review_id": latest_result.get("review_id"),
                "kind": "code",
                "head_sha": latest_result.get("head_sha"),
                "base_sha": latest_result.get("base_sha"),
                "definition_digest": latest_result.get("definition_digest"),
                "verdict": latest_result.get("verdict"),
                "result_path": result_path,
                "result_digest": result_digest,
                "usage": {},
                "migrated": True,
            }
    dispositions: dict[str, dict[str, Any]] = {}
    for item in legacy.get("finding_dispositions", []):
        if isinstance(item, dict) and isinstance(item.get("finding_id"), str):
            dispositions[item["finding_id"]] = item
    for finding_id, finding in findings.items():
        if finding_id in dispositions:
            finding["status"] = dispositions[finding_id].get("status", "open")
    current_candidate = next(
        (
            item
            for item in reversed(legacy.get("candidate_runs", []))
            if isinstance(item, dict) and item.get("status") == "completed"
        ),
        None,
    )
    candidate = None
    if isinstance(current_candidate, dict):
        candidate = {
            "run_id": current_candidate.get("run_id"),
            "head_sha": current_candidate.get("head_sha"),
            "base_sha": current_candidate.get("review_base_sha"),
            "definition_digest": current_candidate.get("definition_digest"),
            "source_digest": current_candidate.get("source_digest"),
            "status": "ready",
            "evidence": [
                {
                    "command_id": command.get("command_id"),
                    "status": command.get("status"),
                    "head_sha": current_candidate.get("head_sha"),
                    "output_digest": command.get("output_digest"),
                }
                for command in current_candidate.get("commands", [])
                if isinstance(command, dict)
            ],
        }
    state = {
        "schema_version": STATE_SCHEMA,
        "revision": 1,
        "change": {
            "id": change_id,
            "slug": str(legacy.get("slug") or change_id.lower()),
            "kind": legacy.get("work_kind") if legacy.get("work_kind") in KINDS else "feature",
            "control": legacy.get("control_level") if legacy.get("control_level") in CONTROLS else "standard",
            "impact_tags": sorted(set(legacy.get("impact_tags", []))),
            "artifacts": artifacts,
            "requirement_prefixes": legacy.get("requirement_prefixes", []),
            "design": (
                {
                    "mode": legacy["design_source"].get("mode"),
                    "kind": (legacy["design_source"].get("source") or {}).get("kind"),
                    "ref": (legacy["design_source"].get("source") or {}).get("ref"),
                    "version": (legacy["design_source"].get("source") or {}).get("version"),
                    "rationale": (legacy["design_source"].get("bypass") or {}).get("rationale"),
                }
                if isinstance(legacy.get("design_source"), dict)
                else None
            ),
        },
        "phase": legacy.get("phase") if legacy.get("phase") in {"definition", "implementation", "review", "accepted"} else "definition",
        "lifecycle": legacy.get("lifecycle") if isinstance(legacy.get("lifecycle"), str) else "draft",
        "approvals": approvals,
        "tickets": tickets,
        "dependencies": dependencies,
        "candidate": candidate,
        "definition_review": None,
        "review": review,
        "findings": findings,
        "acceptance": next(
            (
                item["id"]
                for item in reversed(approvals)
                if item["decision"] == "accept" and item["status"] == "current"
            ),
            None,
        ),
        "active_run": None,
        "migration": {
            "from_schema": 1,
            "source_digest": stable_digest(legacy),
            "migrated_at": utc_now(),
            "discarded_operations": len(legacy.get("operations", [])),
            "discarded_candidate_runs": max(0, len(legacy.get("candidate_runs", [])) - (1 if candidate else 0)),
            "legacy_review_count": len(legacy.get("reviews", [])),
        },
    }
    current_definition = next(
        (
            item
            for item in reversed(approvals)
            if item["decision"] == "definition" and item["status"] == "current"
        ),
        None,
    )
    if current_definition is not None:
        decision_digests: dict[str, str] = {}
        architecture_digest = current_definition.get("architecture_decision_digest")
        design_digest = current_definition.get("design_decision_digest")
        if isinstance(architecture_digest, str):
            decision_digests["architecture"] = architecture_digest
        if isinstance(design_digest, str):
            decision_digests["design"] = design_digest
        state["definition_review"] = {
            "review_id": "legacy-approved-definition",
            "verdict": "review-clear",
            "head_sha": current_definition.get("git_sha"),
            "definition_digest": current_definition["digest"],
            "decision_digests": decision_digests,
            "provenance": "legacy-approved-definition",
        }
    return validate_state(state)


def _rebase_legacy_definition_digest(root: Path, state: dict[str, Any]) -> bool:
    migration = state.get("migration")
    if not isinstance(migration, dict) or migration.get("from_schema") != 1:
        return False
    if migration.get("definition_digest_rebase_contract") == DEFINITION_DIGEST_REBASE_CONTRACT:
        return False
    current_digest = definition_digest(root, state)
    current_definition = next(
        (
            item
            for item in reversed(state["approvals"])
            if item.get("decision") == "definition" and item.get("status") == "current"
        ),
        None,
    )
    migration["definition_digest_rebase_contract"] = DEFINITION_DIGEST_REBASE_CONTRACT
    migration["definition_digest"] = current_digest
    if current_definition is None:
        migration["definition_digest_rebase_status"] = "not-applicable"
        return True
    approved_sha = current_definition.get("git_sha")
    if not isinstance(approved_sha, str) or not SHA_RE.fullmatch(approved_sha):
        raise IntegrityError(
            f"{state['change']['id']} current legacy definition approval has no exact Git revision"
        )
    approved_digest = definition_digest(root, state, revision=approved_sha)
    legacy_digest = current_definition.get("digest")
    if not isinstance(legacy_digest, str):
        raise IntegrityError("Legacy definition approval digest is missing")
    migration["legacy_definition_digest"] = legacy_digest
    if approved_digest != current_digest:
        migration["definition_digest_rebase_status"] = "source-changed"
        return True

    for approval in state["approvals"]:
        if (
            approval.get("status") == "current"
            and approval.get("decision") in {"definition", "accept"}
            and approval.get("digest") == legacy_digest
        ):
            approval["legacy_digest"] = legacy_digest
            approval["digest"] = current_digest
    definition_review = state.get("definition_review")
    if (
        isinstance(definition_review, dict)
        and definition_review.get("provenance") == "legacy-approved-definition"
        and definition_review.get("definition_digest") == legacy_digest
    ):
        definition_review["legacy_definition_digest"] = legacy_digest
        definition_review["definition_digest"] = current_digest
    for key in ("candidate", "review"):
        record = state.get(key)
        if isinstance(record, dict) and record.get("definition_digest") == legacy_digest:
            record["legacy_definition_digest"] = legacy_digest
            record["definition_digest"] = current_digest
    migration["definition_digest_rebase_status"] = "rebased"
    return True


def _rebase_legacy_dependency_digests(projected: dict[str, dict[str, Any]]) -> set[str]:
    changed: set[str] = set()
    mappings: dict[str, tuple[str, str]] = {}
    for change_id, entry in projected.items():
        migration = entry["value"].get("migration")
        if (
            isinstance(migration, dict)
            and migration.get("definition_digest_rebase_status") == "rebased"
            and isinstance(migration.get("legacy_definition_digest"), str)
            and isinstance(migration.get("definition_digest"), str)
        ):
            mappings[change_id] = (
                migration["legacy_definition_digest"],
                migration["definition_digest"],
            )
    for change_id, entry in projected.items():
        for dependency in entry["value"]["dependencies"]:
            mapping = mappings.get(dependency["change_id"])
            if mapping is None or dependency.get("target_definition_digest") != mapping[0]:
                continue
            dependency["legacy_target_definition_digest"] = mapping[0]
            dependency["target_definition_digest"] = mapping[1]
            changed.add(change_id)
    return changed


def upgrade(root: Path, *, apply: bool) -> dict[str, Any]:
    from .worktrees import migrate_registry, resolve_change_root

    state_dir = root / ".dls" / "state"
    paths = sorted(state_dir.glob("*.json"))
    if not paths:
        raise IntegrityError("No DLS state found")
    projected: dict[str, dict[str, Any]] = {}
    for path in paths:
        root_value = read_json(path)
        change_id = str(
            root_value.get("change_id")
            or (root_value.get("change") or {}).get("id")
            or path.stem
        )
        try:
            owner = resolve_change_root(root, change_id)
        except IntegrityError:
            owner = root
        owner_path = state_path(owner, change_id)
        source_path = owner_path if owner_path.is_file() else path
        source = read_json(source_path)
        if source.get("schema_version") == STATE_SCHEMA:
            source_schema = STATE_SCHEMA
            value = copy.deepcopy(validate_state(source))
        elif source.get("schema_version") == 1:
            source_schema = 1
            value = project_legacy_state(owner, source)
        else:
            raise IntegrityError(f"Unsupported state schema: {source_path}")
        repaired = _rebase_legacy_definition_digest(owner, value)
        targets = list(dict.fromkeys([path, owner_path]))
        projected[change_id] = {
            "value": value,
            "targets": targets,
            "source_schema": source_schema,
            "changed": source_schema == 1 or repaired,
        }
    dependency_repairs = _rebase_legacy_dependency_digests(projected)
    for change_id in dependency_repairs:
        projected[change_id]["changed"] = True
    for entry in projected.values():
        if entry["source_schema"] == STATE_SCHEMA and entry["changed"]:
            entry["value"]["revision"] += 1
    upgraded = sum(entry["source_schema"] == 1 for entry in projected.values())
    repaired = sum(
        entry["source_schema"] == STATE_SCHEMA and entry["changed"]
        for entry in projected.values()
    )
    current = len(projected) - upgraded - repaired
    registry = migrate_registry(root, apply=False)
    summary = {
        "ok": True,
        "dry_run": not apply,
        "total_changes": len(paths),
        "already_current": current,
        "to_upgrade": upgraded,
        "to_repair": repaired,
        "dependencies": sum(len(entry["value"]["dependencies"]) for entry in projected.values()),
        "worktree_owners": registry["owners"],
    }
    if not apply:
        return summary
    lock = root / ".dls" / "upgrade.lock"
    with FileLock(lock):
        dls_roots = {
            target.parents[1]
            for entry in projected.values()
            for target in entry["targets"]
        }
        markers = [dls_root / "upgrade-incomplete" for dls_root in dls_roots]
        for marker in markers:
            atomic_write_text(marker, "v0.11 state conversion in progress\n", backup=False)
        try:
            for entry in projected.values():
                for target in entry["targets"]:
                    archive = target.parents[1] / "archive" / "pre-0.11" / "state"
                    archive.mkdir(parents=True, exist_ok=True)
                    backup = archive / target.name
                    if target.is_file() and not backup.exists():
                        shutil.copy2(target, backup)
                    atomic_write_json(target, entry["value"], backup=False)
            migrate_registry(root, apply=True)
        except Exception:
            raise
        else:
            for marker in markers:
                marker.unlink(missing_ok=True)
    return {
        **summary,
        "dry_run": False,
        "upgraded": upgraded,
        "repaired": repaired,
        "archive": ".dls/archive/pre-0.11",
    }


def init_repository(root: Path, *, dry_run: bool) -> dict[str, Any]:
    root = root.resolve()
    config = root / ".dls" / "config.toml"
    created = not config.exists()
    if not dry_run:
        copy_asset(TEMPLATES_ROOT / "config.toml", config)
        (root / ".dls" / "state").mkdir(parents=True, exist_ok=True)
        (root / ".dls" / "reviews").mkdir(parents=True, exist_ok=True)
        ignore = root / ".dls" / ".gitignore"
        if not ignore.exists():
            copy_asset(TEMPLATES_ROOT / "dls.gitignore", ignore)
    return {"ok": True, "dry_run": dry_run, "created": created, "root": str(root)}


def doctor(root: Path) -> dict[str, Any]:
    config = load_config(root)
    state_paths = sorted((root / ".dls" / "state").glob("*.json"))
    schemas = sorted(
        {
            read_json(path).get("schema_version")
            for path in state_paths
            if path.is_file()
        },
        key=str,
    )
    return {
        "ok": schemas in ([], [STATE_SCHEMA]),
        "root": str(root),
        "state_schema": STATE_SCHEMA,
        "state_count": len(state_paths),
        "schemas": schemas,
        "profile": config["default_profile"],
        "next_action": (
            {"id": "upgrade"}
            if schemas and schemas != [STATE_SCHEMA]
            else {"id": "ready"}
        ),
    }


def create_change(
    root: Path,
    *,
    change_id: str,
    slug: str,
    title: str,
    kind: str,
    control: str,
    impact_tags: list[str],
    outcome: str,
    with_tickets: bool,
    with_adr: bool,
    dry_run: bool,
) -> dict[str, Any]:
    config = load_config(root)
    directory = f"{config['docs_root']}/{change_id}-{slug}"
    artifacts: dict[str, dict[str, Any]]
    files: dict[str, str]
    common = {
        "ID": change_id,
        "TITLE": title,
        "KIND": kind,
        "OUTCOME": outcome,
        "SCOPE_ITEM": "Define the smallest deliverable slice.",
        "NON_GOAL": "Unrelated product changes.",
        "REQUIREMENT": "The accepted outcome is demonstrably satisfied.",
        "APPROACH": "Document the chosen approach and rejected alternatives.",
        "VALIDATION": "Run the repository-owned required commands.",
        "RISK_RATIONALE": f"Control level: {control}.",
        "UI_SOURCE": "Not applicable unless the change affects user-interface surfaces.",
        "DISCOVERY": "Record current behavior and owned boundaries before implementation.",
        "INTERFACES": "Document changed interfaces, state, and failure behavior.",
        "CROSS_CUTTING": "Document relevant security, privacy, data, and operational effects.",
    }
    if control in {"standard", "critical"}:
        files = {
            "epic": f"{directory}/EPIC.md",
            "spec": f"{directory}/SPEC.md",
        }
        content = {
            files["epic"]: render_template(
                "EPIC.md",
                {
                    **common,
                    "SUCCESS_MEASURE": "The accepted requirements pass current validation.",
                    "DEPENDENCY": "None.",
                },
            ),
            files["spec"]: render_template("SPEC.md", common),
        }
    else:
        files = {"change": f"{directory}/CHANGE.md"}
        content = {files["change"]: render_template("CHANGE.md", common)}
    if with_tickets:
        files["tickets"] = f"{directory}/TICKETS.md"
        content[files["tickets"]] = render_template(
            "TICKETS.md",
            {
                **common,
                "CONTRACT_FILE": "SPEC.md" if "spec" in files else "CHANGE.md",
                "TICKET_TITLE": "Implement the accepted slice",
            },
        )
    if with_adr:
        files["adr"] = f"{directory}/ADR.md"
        content[files["adr"]] = render_template(
            "ADR.md",
            {
                "ADR_ID": "001",
                "TITLE": title,
                "CONTEXT": "Describe the architectural pressure.",
                "DECISION": "Describe the chosen boundary.",
                "ALTERNATIVES": "List the rejected alternatives.",
                "CONSEQUENCES": "Describe the trade-offs and rollback.",
            },
        )
    artifacts = {
        name: {"path": path, "role": "definition"}
        for name, path in files.items()
    }
    tickets = (
        {f"{change_id}-T01": {"status": "planned", "note": None, "updated_at": None}}
        if with_tickets
        else {}
    )
    state = initial_state(
        change_id=change_id,
        slug=slug,
        kind=kind,
        control=control,
        impact_tags=impact_tags,
        artifacts=artifacts,
        tickets=tickets,
        requirement_prefixes=["REQ"],
    )
    if dry_run:
        return {"ok": True, "dry_run": True, "files": sorted(content), "state": state}
    for relative, text in content.items():
        destination = safe_resolve(root, relative)
        if destination.exists():
            raise IntegrityError(f"Refusing to overwrite existing file: {relative}")
        atomic_write_text(destination, text)
    write_state(root, state)
    return {"ok": True, "dry_run": False, "files": sorted(content), "state": state}


def adopt_change(
    root: Path,
    *,
    change_id: str,
    slug: str,
    kind: str,
    control: str,
    impact_tags: list[str],
    artifacts: dict[str, dict[str, Any]],
    tickets: dict[str, dict[str, Any]],
    requirement_prefixes: list[str],
    dry_run: bool,
) -> dict[str, Any]:
    load_config(root)
    for item in artifacts.values():
        safe_resolve(root, item["path"], must_exist=True)
    state = initial_state(
        change_id=change_id,
        slug=slug,
        kind=kind,
        control=control,
        impact_tags=impact_tags,
        artifacts=artifacts,
        tickets=tickets,
        requirement_prefixes=requirement_prefixes,
    )
    if dry_run:
        return {"ok": True, "dry_run": True, "state": state}
    write_state(root, state)
    return {"ok": True, "dry_run": False, "state": state}
