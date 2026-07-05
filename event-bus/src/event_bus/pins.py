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

# Refuse to load a pin larger than this (defends replay against a runaway/corrupt file).
MAX_PIN_BYTES = 5_000_000


class PinError(Exception):
    """A pin is missing, malformed, oversized, or an unsupported version."""


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


def validate_pin(envelope: dict) -> dict:
    """Validate a pin envelope; raise PinError on any structural problem. Returns the
    envelope unchanged on success."""
    if not isinstance(envelope, dict):
        raise PinError("pin is not a JSON object")
    version = envelope.get("version")
    if version != PIN_SCHEMA_VERSION:
        raise PinError(f"unsupported pin version: {version!r} (expected {PIN_SCHEMA_VERSION})")
    plan = envelope.get("plan")
    if not isinstance(plan, dict):
        raise PinError("pin.plan is missing or not an object")
    stories = plan.get("stories")
    if not isinstance(stories, list) or not stories:
        raise PinError("pin.plan.stories must be a non-empty list")
    for i, story in enumerate(stories):
        if not isinstance(story, dict) or not str(story.get("title", "")).strip():
            raise PinError(f"pin.plan.stories[{i}] is missing a title")
    return envelope


def read_pin(path: Path | str) -> dict:
    """Read + validate a pin file, returning the full envelope. Fails fast (PinError)
    on missing, oversized, unreadable, or malformed pins."""
    path = Path(path)
    if not path.is_file():
        raise PinError(f"pin not found: {path}")
    size = path.stat().st_size
    if size > MAX_PIN_BYTES:
        raise PinError(f"pin too large: {size} bytes (max {MAX_PIN_BYTES})")
    try:
        envelope = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise PinError(f"pin unreadable: {exc}") from exc
    return validate_pin(envelope)


def load_pin(ref: str, base_dir: Path | None = None) -> dict:
    """Resolve a pin reference (an item id, or a path ending in .json / an existing
    file) and return its plan dict, ready for replay. Raises PinError if invalid."""
    p = Path(ref)
    if p.suffix == ".json" or (p.exists() and p.is_file()):
        path = p
    else:
        path = pin_path(str(ref), base_dir)
    return read_pin(path)["plan"]
