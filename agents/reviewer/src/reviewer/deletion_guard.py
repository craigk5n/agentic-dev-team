"""HS-4: "no unexpected deletions" guardrail.

The costliest, most dangerous failures in the DEVHUB build were the coder DELETING
existing files it shouldn't have (#117 deleted the Dockerfile, #111 mass-deleted
config/templates/tests, #81 removed health logic). A diff review can miss this — a
deletion is easy to skim past — so this module turns the PR's changed-file list into a
deterministic signal that the gate can BLOCK on.

Pure functions over Forgejo's PR "files" list (``{filename, status, additions,
deletions}``): which tracked files the PR removes outright or guts, and whether the
story's stated intent (refactor / cleanup / migration) makes that expected. Unexpected
deletions block; expected ones are surfaced but allowed.
"""
from __future__ import annotations

# Words in the story/PR text that make removing files an expected, intended outcome.
_REFACTOR_MARKERS = (
    "refactor", "cleanup", "clean up", "remove", "removal", "delete", "deprecat",
    "migrat", "rename", "consolidat", "prune", "replace", "drop ", "tidy",
)

# A "modified" file that loses this many lines while adding almost nothing back has been
# gutted, not edited — treated the same as an outright deletion of that content.
_GUT_MIN_DELETIONS = 40
_GUT_ADD_RATIO = 0.25   # additions <= 25% of deletions → net removal, not a rewrite


def is_refactor_intent(text: str) -> bool:
    """True if the story/PR text signals that removing files is intended."""
    low = (text or "").lower()
    return any(m in low for m in _REFACTOR_MARKERS)


def analyze_deletions(files: list[dict]) -> dict:
    """From a Forgejo PR files list, return ``{"removed": [...], "gutted": [...]}`` —
    files deleted outright, and modified files that lost most of their content."""
    removed: list[str] = []
    gutted: list[str] = []
    for f in files or []:
        name = f.get("filename") or f.get("name") or ""
        if not name:
            continue
        status = (f.get("status") or "").lower()
        dels = int(f.get("deletions") or 0)
        adds = int(f.get("additions") or 0)
        if status in ("deleted", "removed"):
            removed.append(name)
        elif status == "modified" and dels >= _GUT_MIN_DELETIONS and adds <= _GUT_ADD_RATIO * dels:
            gutted.append(name)
    return {"removed": removed, "gutted": gutted}


def deletion_guardrail(files: list[dict], intent_text: str) -> dict:
    """Deterministic guardrail verdict for a PR's deletions.

    Returns ``{concern, block, removed, gutted, message}``. ``concern`` is True when the
    PR removes or guts tracked files; ``block`` is True only when those deletions are
    *unexpected* — i.e. the story is not a refactor/cleanup. The ``message`` is written to
    be dropped straight into a review comment or the reviewer prompt.
    """
    a = analyze_deletions(files)
    removed, gutted = a["removed"], a["gutted"]
    if not removed and not gutted:
        return {"concern": False, "block": False, "removed": [], "gutted": [], "message": ""}

    parts: list[str] = []
    if removed:
        parts.append("deletes tracked file(s): " + ", ".join(removed[:10]))
    if gutted:
        parts.append("removes most of the content of: " + ", ".join(gutted[:10]))
    detail = "; ".join(parts)

    if is_refactor_intent(intent_text):
        return {"concern": True, "block": False, "removed": removed, "gutted": gutted,
                "message": f"This PR {detail} — allowed because the story's intent is a "
                           "refactor/cleanup, but verify each removal is deliberate."}
    return {"concern": True, "block": True, "removed": removed, "gutted": gutted,
            "message": f"This PR {detail}, but its story is not a refactor/cleanup. "
                       "Unexpected deletion of existing files is blocked — restore the "
                       "files, or state the removal explicitly in the story if intended."}
