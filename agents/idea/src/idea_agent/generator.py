"""LLM-based idea expansion: one-liner prompt → structured proposal."""

from __future__ import annotations
import json
import re

import litellm
import structlog

log = structlog.get_logger()

_SYSTEM = (
    "You are a product manager writing clear, implementable feature proposals. "
    "Be specific about what to build, why it matters, and what success looks like. "
    "Keep descriptions concise — a coding agent will read these."
)

_PROMPT = """\
Expand this feature request into a structured proposal:

"{prompt}"

Respond ONLY with valid JSON (no markdown fences):
{{
  "title": "<descriptive title, max 80 chars>",
  "description": "## Overview\\n\\n<2-3 sentences>\\n\\n## Goals\\n\\n- <goal>\\n\\n## Acceptance Criteria\\n\\n- <criterion>\\n\\n## Out of Scope\\n\\n- <exclusion>"
}}
"""


def expand_prompt(prompt: str, *, model: str, api_key: str = "", redis_conn=None) -> dict:
    """Call LLM to turn a one-liner into a structured idea dict."""
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _PROMPT.format(prompt=prompt)},
        ],
        "temperature": 0.3,
    }
    if api_key:
        kwargs["api_key"] = api_key

    resp = litellm.completion(**kwargs)
    if redis_conn is not None:
        try:
            from reviewer.telemetry import record_usage
            record_usage(redis_conn, "idea", model, resp)
        except Exception:
            pass
    raw = resp.choices[0].message.content or ""
    return _parse(raw, prompt)


def _parse(raw: str, fallback_prompt: str) -> dict:
    clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(clean)
        if "title" in data and "description" in data:
            return data
    except json.JSONDecodeError:
        log.warning("idea_generator_json_error", raw=raw[:200])
    # Graceful fallback — use the raw prompt as title
    return {"title": fallback_prompt[:80], "description": raw or fallback_prompt}


def description_to_html(markdown: str) -> str:
    """Minimal markdown → Plane HTML conversion (headings + lists)."""
    lines = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            lines.append(f"<h2>{line[3:].strip()}</h2>")
        elif line.startswith("- "):
            lines.append(f"<li>{line[2:].strip()}</li>")
        elif line.strip():
            lines.append(f"<p>{line.strip()}</p>")
    return "\n".join(lines)
