"""
Schemas for the stack & SDLC catalog (Story 1.1, 1.2).

Definitions are config-driven data (YAML files), validated against these pydantic
models so a malformed definition fails loudly with a clear error instead of
silently breaking provisioning or planning later.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

# Catalog ids are slugs: lowercase alphanumerics and hyphens.
_SLUG = r"^[a-z0-9][a-z0-9-]*$"


class StackDefinition(BaseModel):
    """One tech stack the Planner can propose for a project."""
    model_config = {"extra": "forbid"}

    id: str = Field(pattern=_SLUG)
    display_name: str
    # Docker image CI runs lint/test/build in.
    ci_image: str
    # Docker image the coding agent (and tester) run in — coder base + toolchain.
    coder_image: str
    # Body of .forgejo/workflows/ci.yml committed to provisioned repos.
    ci_workflow: str
    # Starter files committed at provisioning: repo-relative path -> file contents.
    scaffold: dict[str, str] = Field(default_factory=dict)
    # Language idioms/best practices injected into coder + reviewer prompts.
    best_practices_prompt: str = ""
    # Shell command the coding agent runs in-sandbox to verify its work before
    # opening the PR (TDD red->green). Empty = skip in-coder testing for this stack.
    test_command: str = ""
    # SDLC style used when none is explicitly chosen.
    default_sdlc: str = "standard"
    # Optional file globs used to auto-detect this stack (fallback path).
    detect: list[str] = Field(default_factory=list)

    @field_validator("scaffold")
    @classmethod
    def _scaffold_paths_relative(cls, v: dict[str, str]) -> dict[str, str]:
        for path in v:
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError(f"scaffold path must be repo-relative: {path!r}")
        return v


class SdlcStyle(BaseModel):
    """A cross-cutting development style that shapes story decomposition."""
    model_config = {"extra": "forbid"}

    id: str = Field(pattern=_SLUG)
    display_name: str
    # Instruction injected into the Planner prompt to shape decomposition.
    planner_directive: str
    # Optional instruction injected into the coder prompt (e.g. TDD red->green).
    coder_directive: str = ""
    # Free-text hint describing story ordering rules (e.g. "tests before impl").
    story_ordering: str = ""
