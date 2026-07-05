"""Plan pinning — freeze a planner decomposition to disk for later replay (EPIC 1).

A "pin" is a JSON envelope wrapping the exact plan dict produced by the planner
({project_name, module_name, epics, stories}) plus provenance (schema version, the
planner model used). Story 1.1 covers writing pins; replay (Story 1.2) consumes them.

Pins live under ``settings.pins_dir`` (default ``experiments/pins``, override with the
``PINS_DIR`` env var), one file per idea/item: ``<pins_dir>/<item_id>.json``.
"""
from __future__ import annotations

import json
from pathlib import Path

from event_bus.config import settings

# Bump when the pin envelope shape changes; replay validates against this.
PIN_SCHEMA_VERSION = 1


def pins_dir() -> Path:
    """Resolve the configured base directory for pins (read at call time)."""
    return Path(settings.pins_dir)


def pin_path(item_id: str, base_dir: Path | None = None) -> Path:
    """Absolute path of the pin file for ``item_id``."""
    base = Path(base_dir) if base_dir is not None else pins_dir()
    return base / f"{item_id}.json"


def build_pin(item_id: str, plan: dict, planner_model: str | None) -> dict:
    """Wrap a plan dict in the pin envelope. The plan is stored verbatim so replay
    reproduces the exact story tree that was persisted."""
    return {
        "version": PIN_SCHEMA_VERSION,
        "item_id": item_id,
        "planner_model": planner_model or "",
        "plan": plan,
    }


def write_pin(item_id: str, plan: dict, planner_model: str | None,
              base_dir: Path | None = None) -> Path:
    """Serialize a plan to ``<pins_dir>/<item_id>.json`` atomically (write-then-rename).
    Returns the written path."""
    path = pin_path(item_id, base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_pin(item_id, plan, planner_model)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(path)  # atomic on POSIX; never leaves a half-written pin
    return path
