"""Claude Code subscription adapter for planning calls.

Routes a planning completion through the local ``claude`` CLI in headless print mode
(``claude -p --output-format json``) so it draws on the operator's Claude Code
subscription (via ``CLAUDE_CODE_OAUTH_TOKEN`` from ``claude setup-token``, or the
host login) instead of per-token API billing.

Scope note: this is deliberately for the PLANNER role only — a handful of calls per
project, well within subscription limits. The high-volume coder/reviewer fleet must
stay on API/free models (see CLAUDE.md: heavy headless subscription usage hits the
weekly caps and hard-stops).

Model strings: ``claude-code`` (→ sonnet), ``claude-code/sonnet``,
``claude-code/opus``, ``claude-code/haiku``.
"""

from __future__ import annotations

import json
import os
import subprocess

import structlog

log = structlog.get_logger()

# Overridable so containers can point at a mounted binary.
_CLI = os.environ.get("CLAUDE_CODE_BIN", "claude")

_ALIASES = {"sonnet", "opus", "haiku"}


def is_claude_code_model(model: str) -> bool:
    return (model or "").strip().startswith("claude-code")


def model_alias(model: str) -> str:
    """'claude-code/opus' → 'opus'; bare 'claude-code' defaults to sonnet."""
    _, _, alias = (model or "").partition("/")
    alias = alias.strip().lower()
    return alias if alias in _ALIASES else "sonnet"


def complete(messages: list[dict], *, model: str, timeout: float = 90.0) -> str:
    """One text completion via the claude CLI. Returns the raw response text.

    The prompt is fed on stdin (avoids argv size limits for large plans). The CLI's
    ``--output-format json`` envelope carries the text in ``result`` plus usage
    metadata, which we log (subscription usage has no per-token dollar cost to record).
    Raises RuntimeError on CLI/auth errors and TimeoutError on timeout — both retried
    by the caller's existing retry loop.
    """
    # Planning calls are a single system+user pair; fold them into one prompt.
    prompt = "\n\n".join(m.get("content", "") for m in messages if m.get("content"))

    cmd = [_CLI, "-p", "--output-format", "json", "--model", model_alias(model)]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=timeout, cwd="/tmp",  # neutral cwd: no project CLAUDE.md/permissions
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"claude CLI not found ({_CLI!r}) — mount the binary or set CLAUDE_CODE_BIN"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"claude CLI timed out after {timeout}s") from exc

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}"
        )

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"claude CLI returned non-JSON output: {proc.stdout[:200]}") from exc

    if envelope.get("is_error") or envelope.get("subtype") not in (None, "success"):
        raise RuntimeError(f"claude CLI error result: {str(envelope)[:300]}")

    usage = envelope.get("usage") or {}
    log.info("claude_code_completion", model=model_alias(model),
             input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
             duration_ms=envelope.get("duration_ms"), num_turns=envelope.get("num_turns"))
    return envelope.get("result") or ""
