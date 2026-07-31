"""Deterministic core for the DLS Codex plugin."""

import json
from pathlib import Path


def _plugin_version() -> str:
    manifest = Path(__file__).resolve().parents[2] / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    version = payload.get("version")
    return version if isinstance(version, str) and version else "unknown"


VERSION = _plugin_version()
SCHEMA_VERSION = 2
