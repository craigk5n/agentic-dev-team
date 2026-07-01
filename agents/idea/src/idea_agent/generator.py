"""LLM-based idea expansion: one-liner prompt → structured proposal."""

from __future__ import annotations
import json
import re
import time

import litellm
import structlog

log = structlog.get_logger()

# Bounded timeout + retry so a slow/erroring provider can't hang idea expansion.
_CALL_TIMEOUT = 90.0
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF = 2.0

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
  "description": "## Overview\\n\\n<2-3 sentences>\\n\\n## Goals\\n\\n- <goal>\\n\\n## Acceptance Criteria\\n\\n- <criterion>\\n\\n## Out of Scope\\n\\n- <exclusion>"{stack_fields}
}}
{stack_guidance}"""

_STACK_FIELDS = (
    ',\n  "proposed_stack": "<one stack id from the list>",'
    '\n  "proposed_sdlc": "<one sdlc id from the list>",'
    '\n  "stack_rationale": "<one sentence on why this stack fits>"'
)
_STYLE_FIELD = ',\n  "proposed_style_guides": ["<zero or more style-guide ids from the list>"]'
_DECISIONS_FIELD = (
    ',\n  "design_decisions": [{{"question": "<a key design choice for THIS project>", '
    '"recommended": "<your recommended option>", "rationale": "<one sentence why>", '
    '"alternatives": ["<other viable option>", "<another>"]}}]'
)
_DECISIONS_GUIDANCE = (
    "\nAlso surface 4 to 7 DESIGN DECISIONS a human should confirm before building — "
    "the choices that most shape the implementation (architecture, storage/DB, auth, "
    "key libraries, sync vs async, scope boundaries). For each, give your RECOMMENDED "
    "option, a one-sentence rationale, and 2-3 concrete alternatives. Make them specific "
    "to THIS project, not generic."
)


def _stack_guidance(stack_options: list[dict], sdlc_options: list[dict]) -> str:
    if not stack_options:
        return ""
    stacks = ", ".join(f"{s['id']} ({s.get('display_name', s['id'])})" for s in stack_options)
    sdlc = ", ".join(f"{s['id']} ({s.get('display_name', s['id'])})" for s in sdlc_options)
    return (
        f"\nChoose the best-fitting tech stack and development style.\n"
        f"Available stacks (use the id): {stacks}.\n"
        f"Available SDLC styles (use the id): {sdlc}.\n"
        f"If no stack clearly fits, choose 'generic'."
    )


def _style_guidance(style_guide_options: list[dict]) -> str:
    if not style_guide_options:
        return ""
    guides = ", ".join(f"{s['id']} ({s.get('display_name', s['id'])})" for s in style_guide_options)
    return (
        f"\nAlso recommend any code-style guides that fit this project (use the ids; "
        f"choose zero or more). Available: {guides}."
    )


def expand_prompt(prompt: str, *, model: str, api_key: str = "", redis_conn=None,
                  stack_options: list[dict] | None = None,
                  sdlc_options: list[dict] | None = None,
                  style_guide_options: list[dict] | None = None) -> dict:
    """Call LLM to turn a one-liner into a structured idea dict.

    When stack_options/sdlc_options are given, the proposal also includes
    proposed_stack, proposed_sdlc, and stack_rationale (constrained to the lists).
    style_guide_options adds proposed_style_guides (a list).
    """
    stack_options = stack_options or []
    sdlc_options = sdlc_options or []
    style_guide_options = style_guide_options or []
    stack_fields = ((_STACK_FIELDS if stack_options else "")
                    + (_STYLE_FIELD if style_guide_options else "") + _DECISIONS_FIELD)
    user_msg = _PROMPT.format(
        prompt=prompt,
        stack_fields=stack_fields,
        stack_guidance=(_stack_guidance(stack_options, sdlc_options)
                        + _style_guidance(style_guide_options) + _DECISIONS_GUIDANCE),
    )
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.3,
        "timeout": _CALL_TIMEOUT,   # fail fast instead of litellm's long backoff
        "num_retries": 0,
    }
    if api_key:
        kwargs["api_key"] = api_key

    # Bounded retry — free models sometimes hang or return empty/error content.
    raw = ""
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = litellm.completion(**kwargs)
            if redis_conn is not None:
                try:
                    from reviewer.telemetry import record_usage
                    record_usage(redis_conn, "idea", model, resp)
                except Exception:
                    pass
            raw = resp.choices[0].message.content or ""
            data = _try_parse(raw)
            if data is not None:
                return data
            raise ValueError("empty or unparseable response")
        except Exception as exc:
            log.warning("idea_llm_retry", attempt=attempt, max=_MAX_ATTEMPTS, error=str(exc)[:140])
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF * attempt)
    return _parse(raw, prompt)   # graceful fallback after retries


def _try_parse(raw: str) -> dict | None:
    """Parse a valid proposal (title + description) or None."""
    clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(clean)
        if isinstance(data, dict) and data.get("title") and data.get("description"):
            return data
    except json.JSONDecodeError:
        pass
    return None


def _parse(raw: str, fallback_prompt: str) -> dict:
    data = _try_parse(raw)
    if data is not None:
        return data
    log.warning("idea_generator_json_error", raw=raw[:200])
    # Graceful fallback — use the raw prompt as title
    return {"title": fallback_prompt[:80], "description": raw or fallback_prompt}


def description_to_html(markdown: str) -> str:
    """Minimal markdown → HTML conversion (headings + lists)."""
    lines = []
    for line in markdown.splitlines():
        if line.startswith("## "):
            lines.append(f"<h2>{line[3:].strip()}</h2>")
        elif line.startswith("- "):
            lines.append(f"<li>{line[2:].strip()}</li>")
        elif line.strip():
            lines.append(f"<p>{line.strip()}</p>")
    return "\n".join(lines)
