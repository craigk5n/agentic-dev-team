"""
Stack & SDLC catalog — public API.

The catalog is a config-driven, user-extensible registry of stack definitions and
SDLC styles (see schema.py). Built-in defaults ship with the package; a CATALOG_DIR
can add or override entries. Resolution helpers provide a generic fallback so the
rest of the system never has to special-case "no stack".
"""

from __future__ import annotations

from event_bus.catalog.loader import load_sdlc, load_stacks, load_style_guides
from event_bus.catalog.schema import SdlcStyle, StackDefinition, StyleGuide

GENERIC_STACK_ID = "generic"
DEFAULT_SDLC_ID = "standard"


class Catalog:
    """Loaded stack + SDLC definitions, with lookup + fallback resolution."""

    def __init__(self) -> None:
        self.stacks: dict[str, StackDefinition] = {}
        self.sdlc: dict[str, SdlcStyle] = {}
        self.style_guides: dict[str, StyleGuide] = {}
        self.reload()

    def reload(self) -> None:
        """Re-read definitions from disk (Story 1.5)."""
        self.stacks = load_stacks()
        self.sdlc = load_sdlc()
        self.style_guides = load_style_guides()

    # ── lookups ──────────────────────────────────────────────────────────────
    def list_stacks(self) -> list[StackDefinition]:
        return sorted(self.stacks.values(), key=lambda s: s.id)

    def list_sdlc(self) -> list[SdlcStyle]:
        return sorted(self.sdlc.values(), key=lambda s: s.id)

    def list_style_guides(self) -> list[StyleGuide]:
        return sorted(self.style_guides.values(), key=lambda s: s.id)

    def style_guides_for_stack(self, stack_id: str | None) -> list[StyleGuide]:
        """Style guides applicable to a stack: cross-cutting ones + stack-scoped ones."""
        return [g for g in self.list_style_guides()
                if not g.applies_to_stacks or (stack_id in g.applies_to_stacks)]

    def has_stack(self, stack_id: str) -> bool:
        return stack_id in self.stacks

    def has_sdlc(self, sdlc_id: str) -> bool:
        return sdlc_id in self.sdlc

    def has_style_guide(self, guide_id: str) -> bool:
        return guide_id in self.style_guides

    def get_style_guides(self, ids: list[str]) -> list[StyleGuide]:
        """Resolve a list of guide ids to definitions, ignoring unknown ids, in order."""
        return [self.style_guides[i] for i in ids if i in self.style_guides]

    def get_stack(self, stack_id: str | None) -> StackDefinition:
        """Return the stack, or the generic fallback when unset/unknown (Story 1.6)."""
        if stack_id and stack_id in self.stacks:
            return self.stacks[stack_id]
        return self.stacks[GENERIC_STACK_ID]

    def get_sdlc(self, sdlc_id: str | None) -> SdlcStyle:
        """Return the SDLC style, or the default when unset/unknown."""
        if sdlc_id and sdlc_id in self.sdlc:
            return self.sdlc[sdlc_id]
        return self.sdlc[DEFAULT_SDLC_ID]


_catalog: Catalog | None = None


def get_catalog() -> Catalog:
    global _catalog
    if _catalog is None:
        _catalog = Catalog()
    return _catalog


def reload_catalog() -> Catalog:
    get_catalog().reload()
    return _catalog  # type: ignore[return-value]
