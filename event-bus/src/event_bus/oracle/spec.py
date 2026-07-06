"""Oracle spec format + validation (EPIC 2, Story 2.1).

An oracle spec is a JSON object stored in the reference-project catalog (NOT in the target
repo, so the coder can't fit to it):

    {
      "name": "pastebin-web",
      "backend": "cli" | "browser",
      "checks": [ { ...backend-specific check... }, ... ]
    }

- ``cli`` checks (Story 2.2): golden stdout/exit-code/output-file assertions.
- ``browser`` checks (Story 2.3): Playwright journeys (load, click, assert visible output).
"""
from __future__ import annotations

import json
from pathlib import Path

SUPPORTED_BACKENDS = ("cli", "browser")


class OracleSpecError(Exception):
    """An oracle spec is missing, malformed, or names an unknown backend."""


def validate_spec(spec: dict) -> dict:
    """Validate an oracle spec; raise OracleSpecError on any problem. Returns it unchanged."""
    if not isinstance(spec, dict):
        raise OracleSpecError("oracle spec must be a JSON object")
    name = spec.get("name")
    if not name or not isinstance(name, str):
        raise OracleSpecError("oracle spec requires a non-empty string 'name'")
    backend = spec.get("backend")
    if backend not in SUPPORTED_BACKENDS:
        raise OracleSpecError(
            f"oracle spec 'backend' must be one of {SUPPORTED_BACKENDS}, got {backend!r}")
    checks = spec.get("checks")
    if not isinstance(checks, list) or not checks:
        raise OracleSpecError("oracle spec requires a non-empty 'checks' list")
    return spec


def load_spec(path: str | Path) -> dict:
    """Read + validate an oracle spec file. Fails fast (OracleSpecError) on missing/malformed."""
    p = Path(path)
    if not p.is_file():
        raise OracleSpecError(f"oracle spec not found: {p}")
    try:
        spec = json.loads(p.read_text())
    except (ValueError, OSError) as exc:
        raise OracleSpecError(f"oracle spec unreadable: {exc}") from exc
    return validate_spec(spec)
