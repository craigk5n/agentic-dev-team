"""Oracle runner + backend registry (EPIC 2, Story 2.1).

The runner dispatches a validated spec to a registered backend and normalizes the result
to {name, backend, passed, checks, defects}. A backend is any callable
``(spec: dict, target: dict) -> (checks: list[dict], defects: list[dict])`` where:

- ``target`` describes what to run against, e.g. {"binary": "/path/app"} for cli or
  {"url": "http://host:port"} for browser.
- each ``check`` is {"name": str, "passed": bool, "detail": str}
- each ``defect`` is {"class": str, "description": str, ...} (a failed acceptance)

The overall run passes iff every check passes and no defects were reported.
"""
from __future__ import annotations

import json

from event_bus.oracle.spec import validate_spec


class OracleRunError(Exception):
    """No backend is registered for the spec's backend name."""


# name -> callable(spec, target) -> (checks, defects)
_BACKENDS: dict = {}


def register_backend(name: str, fn) -> None:
    """Register an oracle backend under ``name`` (idempotent overwrite)."""
    _BACKENDS[name] = fn


def get_backend(name: str):
    return _BACKENDS.get(name)


def run_oracle(spec: dict, target: dict, backends: dict | None = None) -> dict:
    """Run a hidden acceptance oracle against a built artifact/endpoint.

    INDEPENDENT of the build/CI (Story 2.1): invoked post-merge only; never touches the
    agent's in-build tests or the merge gate. Returns a machine-readable result."""
    spec = validate_spec(spec)
    registry = backends if backends is not None else _BACKENDS
    backend = registry.get(spec["backend"])
    if backend is None:
        raise OracleRunError(f"no backend registered for {spec['backend']!r}")
    checks, defects = backend(spec, target or {})
    checks = list(checks or [])
    defects = list(defects or [])
    passed = all(c.get("passed") for c in checks) and not defects
    return {
        "name": spec["name"],
        "backend": spec["backend"],
        "passed": bool(passed),
        "checks": checks,
        "defects": defects,
    }


def persist_oracle_result(r, story_id: str, result: dict, run_id: str = "") -> dict:
    """Persist an oracle result linked to a story/run (feeds the EPIC 5 defect ledger).

    Best-effort: stored as JSON at ``oracle:result:{story_id}`` (30-day TTL). Never raises."""
    payload = {**result, "story_id": story_id, "run_id": run_id}
    if r is not None and story_id:
        try:
            r.setex(f"oracle:result:{story_id}", 30 * 86_400, json.dumps(payload))
        except Exception:
            pass
    return payload
