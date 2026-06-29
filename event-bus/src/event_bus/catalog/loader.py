"""
Catalog loader (Story 1.1, 1.2).

Loads stack and SDLC definitions from one or more directories. Each directory may
contain `stacks/*.yaml` and `sdlc/*.yaml`. Built-in defaults ship with the package;
a user directory (CATALOG_DIR) can add or override definitions by id — so users can
add stacks without changing core code. A malformed definition is skipped with a
logged error; the rest still load.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog
import yaml
from pydantic import ValidationError

from event_bus.catalog.schema import SdlcStyle, StackDefinition

log = structlog.get_logger()

# Built-in definitions shipped with the package.
_DEFAULTS_DIR = Path(__file__).parent / "defaults"


def catalog_dirs() -> list[Path]:
    """Directories to load from: built-in defaults, then the optional user dir
    (CATALOG_DIR) which overrides defaults by id."""
    dirs = [_DEFAULTS_DIR]
    user = os.environ.get("CATALOG_DIR", "").strip()
    if user:
        dirs.append(Path(user))
    return dirs


def _load_kind(dirs: list[Path], subdir: str, model) -> dict:
    """Load and validate all `*.yaml`/`*.yml` files in `{dir}/{subdir}` for each dir.
    Later dirs override earlier ones by id."""
    out: dict = {}
    for base in dirs:
        d = base / subdir
        if not d.is_dir():
            continue
        for path in sorted([*d.glob("*.yaml"), *d.glob("*.yml")]):
            try:
                raw = yaml.safe_load(path.read_text()) or {}
                obj = model.model_validate(raw)
            except (ValidationError, yaml.YAMLError, OSError) as exc:
                log.error("catalog_definition_invalid", file=str(path), error=str(exc))
                continue
            if obj.id in out:
                log.info("catalog_definition_overridden", kind=subdir, id=obj.id, file=str(path))
            out[obj.id] = obj
    return out


def load_stacks(dirs: list[Path] | None = None) -> dict[str, StackDefinition]:
    return _load_kind(dirs or catalog_dirs(), "stacks", StackDefinition)


def load_sdlc(dirs: list[Path] | None = None) -> dict[str, SdlcStyle]:
    return _load_kind(dirs or catalog_dirs(), "sdlc", SdlcStyle)
