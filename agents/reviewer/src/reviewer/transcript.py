"""Per-run LLM transcript capture (Story 5.4) for reproducibility.

Appends one redacted JSONL entry per LLM call to
``<transcript_dir>/<run_id>/transcript.jsonl`` — capturing role, model, seed, temperature,
the messages sent, and the response. Only run-scoped calls (experiments) write transcripts;
without a run_id this is a no-op. Secrets are redacted before write and the file is size-
capped with a single-generation rotation.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from reviewer.config import settings

# Rotate once past this size so a long experiment can't fill the disk.
MAX_TRANSCRIPT_BYTES = 50_000_000

# Redact common credential shapes before anything touches disk.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"sk-or-v1-[A-Za-z0-9_\-]{6,}"),
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{10,}"),
]


def redact(text) -> str:
    """Mask API keys / tokens in a string."""
    s = str(text if text is not None else "")
    for pat in _SECRET_PATTERNS:
        s = pat.sub(lambda m: (m.group(1) if m.lastindex else "") + "[REDACTED]", s)
    return s


def transcript_path(run_id: str, base_dir: str | Path | None = None) -> Path:
    base = Path(base_dir) if base_dir is not None else Path(settings.transcript_dir)
    return base / run_id / "transcript.jsonl"


def _redact_entry(entry: dict) -> dict:
    out = {k: (redact(v) if isinstance(v, str) else v) for k, v in entry.items()}
    if isinstance(entry.get("messages"), list):
        out["messages"] = [{**m, "content": redact(m.get("content", ""))}
                           for m in entry["messages"]]
    if "response" in entry:
        out["response"] = redact(entry.get("response"))
    return out


def write_transcript(run_id: str, entry: dict, base_dir: str | Path | None = None) -> Path | None:
    """Append a redacted transcript entry for a run. No-op (returns None) without a run_id.
    Never raises — transcript capture must not break an LLM call."""
    if not run_id:
        return None
    try:
        path = transcript_path(run_id, base_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            path.replace(path.with_suffix(".jsonl.1"))  # single-generation rotation
        with path.open("a") as f:
            f.write(json.dumps(_redact_entry(entry)) + "\n")
        return path
    except Exception:
        return None
