"""Scoped UI/UX and architecture decision contracts.

The functions in this module are deliberately read-only.  State mutations live
in :mod:`dls_core.operations`, while every gate consumes the same projections
and readiness result defined here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import IntegrityError, UsageError
from .io import canonical_file_digest, canonical_text, safe_resolve, sha256_bytes
from .repo import run_git

DESIGN_SOURCE_CONTRACT = "dls-design-source/v1"
DESIGN_DIGEST_CONTRACT = "dls-design-digest/v1"
ARCHITECTURE_DIGEST_CONTRACT = "dls-architecture-digest/v1"

DESIGN_TIERS = {1, 2, 3}
DESIGN_SOURCE_KINDS = {"precedent", "artifact", "external-version"}
UX_RISKS = {"low", "medium", "high"}
SURFACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ARCHITECTURE_START = "<!-- dls:architecture:start -->"
ARCHITECTURE_END = "<!-- dls:architecture:end -->"
ARCHITECTURE_HEADINGS = {"architecture", "architecture and alternatives"}


def _stable_digest(contract: str, payload: dict[str, Any]) -> str:
    return sha256_bytes(
        json.dumps(
            {"contract": contract, **payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def validate_surfaces(values: list[str]) -> list[str]:
    surfaces = sorted(set(values))
    if not surfaces or len(surfaces) > 32:
        raise UsageError("Design source requires 1-32 unique surfaces")
    invalid = [value for value in surfaces if not SURFACE_PATTERN.fullmatch(value)]
    if invalid:
        raise UsageError("Invalid design surfaces: " + ", ".join(invalid))
    return surfaces


def _tracked_source(root: Path, reference: str) -> dict[str, str]:
    candidate = Path(reference)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise IntegrityError(f"Unsafe repository design source path: {reference}")
    path = safe_resolve(root, reference, must_exist=True)
    if path.is_symlink() or not path.is_file():
        raise IntegrityError("Repository design source must be a regular tracked file")
    relative = path.relative_to(root.resolve()).as_posix()
    tracked = run_git(root, "ls-files", "--error-unmatch", "--", relative, check=False)
    if tracked.returncode != 0:
        raise IntegrityError(f"Repository design source is not tracked: {relative}")
    status = run_git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        relative,
        check=False,
    )
    if status.returncode != 0 or status.stdout:
        raise IntegrityError(f"Repository design source must match HEAD: {relative}")
    blob = run_git(root, "rev-parse", f"HEAD:{relative}", check=False)
    git_blob = blob.stdout.strip()
    if blob.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", git_blob):
        raise IntegrityError(f"Repository design source has no exact Git blob: {relative}")
    return {
        "ref": relative,
        "version": f"git:{git_blob}",
        "content_digest": "sha256:" + canonical_file_digest(path),
        "git_blob": git_blob,
    }


def build_design_source(
    root: Path,
    *,
    tier: int,
    surfaces: list[str],
    source_kind: str | None,
    source_ref: str | None,
    source_version: str | None,
    bypass: bool,
    rationale: str | None,
    risk: str | None,
) -> dict[str, Any]:
    if tier not in DESIGN_TIERS:
        raise UsageError("Design tier must be 1, 2, or 3")
    normalized_surfaces = validate_surfaces(surfaces)
    if bypass:
        if source_kind or source_ref or source_version:
            raise UsageError("Design bypass cannot include a source")
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2000:
            raise UsageError("Design bypass requires a rationale of 1-2000 characters")
        if risk not in UX_RISKS:
            raise UsageError("Design bypass requires --risk low|medium|high")
        return {
            "contract": DESIGN_SOURCE_CONTRACT,
            "tier": tier,
            "surfaces": normalized_surfaces,
            "mode": "bypass",
            "source": None,
            "bypass": {"rationale": rationale.strip(), "ux_risk": risk},
        }
    if rationale is not None or risk is not None:
        raise UsageError("Design source mode cannot include bypass rationale or risk")
    if source_kind not in DESIGN_SOURCE_KINDS:
        raise UsageError("Design source requires a supported --source-kind")
    if tier == 3 and source_kind == "precedent":
        raise UsageError(
            "Tier 3 requires an immutable artifact or external version; "
            "use an explicit bypass when no sufficient design source exists"
        )
    if not isinstance(source_ref, str) or not source_ref.strip():
        raise UsageError("Design source requires --source-ref")
    source_ref = source_ref.strip()
    if source_kind == "external-version":
        parsed = urlparse(source_ref)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise IntegrityError("External design source must be a credential-free HTTPS URL")
        if not isinstance(source_version, str) or not source_version.strip():
            raise UsageError("External design source requires an immutable --source-version")
        version = source_version.strip()
        source = {
            "kind": source_kind,
            "ref": source_ref,
            "version": version,
            "content_digest": "sha256:"
            + sha256_bytes(f"{source_ref}\0{version}".encode("utf-8")),
            "git_blob": None,
        }
    else:
        source = {"kind": source_kind, **_tracked_source(root, source_ref)}
        if source_version is not None and source_version.strip() != source["version"]:
            raise IntegrityError("Repository design source version must equal its exact Git blob")
    return {
        "contract": DESIGN_SOURCE_CONTRACT,
        "tier": tier,
        "surfaces": normalized_surfaces,
        "mode": "source",
        "source": source,
        "bypass": None,
    }


def validate_design_source_shape(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("contract") != DESIGN_SOURCE_CONTRACT:
        raise IntegrityError("Unsupported design source contract")
    if set(value) != {"contract", "tier", "surfaces", "mode", "source", "bypass"}:
        raise IntegrityError("Design source fields are invalid")
    tier = value.get("tier")
    if tier not in DESIGN_TIERS:
        raise IntegrityError("Invalid design tier")
    try:
        surfaces = validate_surfaces(value.get("surfaces", []))
    except UsageError as exc:
        raise IntegrityError(str(exc)) from exc
    if surfaces != value.get("surfaces"):
        raise IntegrityError("Design surfaces must be sorted and unique")
    mode = value.get("mode")
    if mode == "bypass":
        bypass = value.get("bypass")
        if value.get("source") is not None or not isinstance(bypass, dict):
            raise IntegrityError("Malformed design bypass")
        if set(bypass) != {"rationale", "ux_risk"}:
            raise IntegrityError("Design bypass fields are invalid")
        rationale = bypass.get("rationale")
        if (
            not isinstance(rationale, str)
            or not rationale.strip()
            or rationale != rationale.strip()
            or len(rationale) > 2000
        ):
            raise IntegrityError("Malformed design bypass rationale")
        if bypass.get("ux_risk") not in UX_RISKS:
            raise IntegrityError("Malformed design bypass risk")
    elif mode == "source":
        source = value.get("source")
        if value.get("bypass") is not None or not isinstance(source, dict):
            raise IntegrityError("Malformed design source")
        if set(source) != {"kind", "ref", "version", "content_digest", "git_blob"}:
            raise IntegrityError("Design source provenance fields are invalid")
        kind = source.get("kind")
        if kind not in DESIGN_SOURCE_KINDS:
            raise IntegrityError("Malformed design source kind")
        if tier == 3 and kind == "precedent":
            raise IntegrityError("Tier 3 design source cannot be a precedent")
        reference = source.get("ref")
        version = source.get("version")
        content_digest = source.get("content_digest")
        if not all(isinstance(item, str) and item for item in (reference, version, content_digest)):
            raise IntegrityError("Design source provenance is incomplete")
        if len(reference) > 2048 or len(version) > 512:
            raise IntegrityError("Design source provenance is oversized")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", content_digest):
            raise IntegrityError("Design source content_digest must be SHA-256")
        if kind == "external-version":
            parsed = urlparse(reference)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise IntegrityError(
                    "External design source must use credential-free HTTPS"
                )
            expected = "sha256:" + sha256_bytes(f"{reference}\0{version}".encode("utf-8"))
            if content_digest != expected or source.get("git_blob") is not None:
                raise IntegrityError("External design source provenance changed")
        elif not isinstance(source.get("git_blob"), str) or not re.fullmatch(
            r"[0-9a-f]{40,64}", source["git_blob"]
        ):
            raise IntegrityError("Repository design source Git blob is invalid")
        elif version != f"git:{source['git_blob']}":
            raise IntegrityError("Repository design source version does not match Git blob")
    else:
        raise IntegrityError("Invalid design source mode")
    return value


def validate_design_source(root: Path, value: object) -> dict[str, Any]:
    validated = validate_design_source_shape(value)
    source = validated.get("source")
    if isinstance(source, dict) and source.get("kind") in {"artifact", "precedent"}:
        reference = source["ref"]
        current = _tracked_source(root, reference)
        for field in ("ref", "version", "content_digest", "git_blob"):
            if source.get(field) != current[field]:
                raise IntegrityError(f"Repository design source changed: {reference}")
    return validated


def design_digest(root: Path, state: dict[str, Any]) -> str | None:
    value = state.get("design_source")
    if value is None:
        return None
    validate_design_source_shape(value)
    try:
        validated = validate_design_source(root, value)
    except IntegrityError:
        return None
    return _stable_digest(DESIGN_DIGEST_CONTRACT, {"design": validated})


def _marked_architecture(text: str) -> str | None:
    starts = [match.start() for match in re.finditer(re.escape(ARCHITECTURE_START), text)]
    ends = [match.start() for match in re.finditer(re.escape(ARCHITECTURE_END), text)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise IntegrityError("Architecture decision region is ambiguous")
    body_start = starts[0] + len(ARCHITECTURE_START)
    body = text[body_start : ends[0]].strip()
    if not body:
        raise IntegrityError("Architecture decision region is empty")
    return body


def _legacy_architecture(text: str) -> str | None:
    matches: list[tuple[int, int]] = []
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", text, re.MULTILINE))
    for index, match in enumerate(headings):
        if match.group(1).strip().lower() not in ARCHITECTURE_HEADINGS:
            continue
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        matches.append((match.start(), end))
    if len(matches) > 1:
        raise IntegrityError("Architecture decision headings are ambiguous")
    if not matches:
        return None
    start, end = matches[0]
    body = text[start:end].strip()
    if not body:
        raise IntegrityError("Architecture decision section is empty")
    return body


def architecture_source(root: Path, state: dict[str, Any]) -> dict[str, Any] | None:
    artifacts = state.get("artifacts", {})
    adr = artifacts.get("adr")
    if isinstance(adr, dict) and isinstance(adr.get("path"), str):
        path = safe_resolve(root, adr["path"], must_exist=True)
        body = canonical_text(path.read_text(encoding="utf-8")).strip()
        if not body:
            raise IntegrityError("Canonical ADR is empty")
        return {"source_kind": "adr", "path": adr["path"], "content": body}
    spec = artifacts.get("spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("path"), str):
        return None
    path = safe_resolve(root, spec["path"], must_exist=True)
    text = canonical_text(path.read_text(encoding="utf-8"))
    marked = _marked_architecture(text)
    if marked is not None:
        return {"source_kind": "spec-marker", "path": spec["path"], "content": marked}
    legacy = _legacy_architecture(text)
    if legacy is not None:
        return {"source_kind": "spec-heading", "path": spec["path"], "content": legacy}
    return None


def architecture_required(state: dict[str, Any]) -> tuple[bool, list[str]]:
    triggers: list[str] = []
    if "architecture" in state.get("impact_tags", []):
        triggers.append("architecture")
    if "adr" in state.get("artifacts", {}):
        triggers.append("adr")
    return bool(triggers), triggers


def architecture_digest(root: Path, state: dict[str, Any]) -> str | None:
    source = architecture_source(root, state)
    if source is None:
        return None
    return _stable_digest(
        ARCHITECTURE_DIGEST_CONTRACT,
        {
            "source_kind": source["source_kind"],
            "content": source["content"],
        },
    )


def _approval_status(
    approvals: list[dict[str, Any]],
    *,
    decision: str,
) -> str:
    relevant = [item for item in approvals if item.get("decision") == decision]
    if any(item.get("status") == "current" for item in relevant):
        return "current"
    if any(item.get("status") == "stale" for item in relevant):
        return "stale"
    return "pending"


def decision_projection(
    root: Path,
    state: dict[str, Any],
    approvals: list[dict[str, Any]],
) -> dict[str, Any]:
    design_value = state.get("design_source")
    design_value = validate_design_source_shape(design_value) if design_value is not None else None
    source = design_value.get("source") if isinstance(design_value, dict) else None
    current_design_digest = design_digest(root, state)
    required, triggers = architecture_required(state)
    architecture = architecture_source(root, state)
    return {
        "design": {
            "required": "user-interface" in state.get("impact_tags", []),
            "contract": DESIGN_SOURCE_CONTRACT if design_value is not None else None,
            "tier": design_value.get("tier") if design_value else None,
            "surfaces": list(design_value.get("surfaces", [])) if design_value else [],
            "mode": design_value.get("mode") if design_value else None,
            "source_kind": source.get("kind") if isinstance(source, dict) else None,
            "bypass": bool(design_value and design_value.get("mode") == "bypass"),
            "digest": current_design_digest,
            "provenance_current": bool(design_value and current_design_digest),
            "approval": _approval_status(approvals, decision="design"),
        },
        "architecture": {
            "required": required,
            "source_kind": architecture.get("source_kind") if architecture else None,
            "digest": architecture_digest(root, state),
            "approval": _approval_status(approvals, decision="architecture"),
            "triggers": triggers,
        },
    }


def decision_readiness(
    root: Path,
    state: dict[str, Any],
    approvals: list[dict[str, Any]],
    *,
    require_definition: bool,
) -> dict[str, Any]:
    projection = decision_projection(root, state, approvals)
    definition_current = any(
        item.get("decision") == "definition" and item.get("status") == "current"
        for item in approvals
    )
    design = projection["design"]
    architecture = projection["architecture"]
    action: dict[str, str] | None = None
    if design["required"] and design["contract"] is None:
        action = {"id": "record-design-source", "detail": "UI change requires a typed design source or bypass"}
    elif design["required"] and not design["provenance_current"]:
        action = {"id": "record-design-source", "detail": "design source provenance no longer matches its exact version"}
    elif architecture["required"] and architecture["digest"] is None:
        action = {"id": "record-architecture-decision", "detail": "architecture trigger requires one bounded ADR or SPEC decision"}
    elif architecture["required"] and architecture["approval"] != "current":
        action = {"id": "approve-architecture", "detail": (architecture["digest"] or "")[:12]}
    elif design["required"] and design["approval"] != "current":
        action = {
            "id": "approve-definition-and-design" if require_definition and not definition_current else "approve-design",
            "detail": (design["digest"] or "")[:12],
        }
    elif require_definition and not definition_current:
        action = {"id": "approve-definition", "detail": "current authored definition approval required"}
    return {
        "contract": "dls-decision-readiness/v1",
        "ready": action is None,
        "next_action": action,
        "decisions": projection,
    }


def review_pack_decisions_current(
    pack_decisions: object,
    current_decisions: dict[str, Any],
) -> bool:
    """Preserve generic legacy packs but require provenance for scoped decisions."""
    if isinstance(pack_decisions, dict):
        return pack_decisions == current_decisions
    design = current_decisions["design"]
    architecture = current_decisions["architecture"]
    requires_projection = bool(
        design.get("required")
        or design.get("contract")
        or architecture.get("required")
    )
    return pack_decisions is None and not requires_projection
