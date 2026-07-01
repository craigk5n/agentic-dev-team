"""LLM-based idea decomposition: approved idea → module + stories."""

from __future__ import annotations
import json
import re

import litellm
import structlog

log = structlog.get_logger()

_SYSTEM = (
    "You are a technical project manager. "
    "Break down feature ideas into small, independently implementable coding tasks. "
    "Each story must be completable in one focused coding session."
)

_PROMPT = """\
Decompose this approved feature into an epic and implementable stories.

Title: {title}
Description:
{description}

Target repository: {default_repo}
{style_block}
Create:
1. One module (epic) grouping all stories
2. Between 3 and 7 stories — each a concrete, implementable task

The target repository is ALREADY scaffolded: build/manifest files, a test harness,
and CI already exist. So:
- Do NOT create setup/scaffolding/"initialize the project" stories — that work is done.
- Every story must deliver a working, independently testable vertical slice — real
  behavior PLUS its tests — that can pass code review on its own.
- NEVER make a story that only declares a function signature, a stub, or a
  placeholder: an unimplemented signature fails review and cannot be fixed within
  its own scope. The FIRST story must implement a minimal but complete and tested
  piece of real functionality.

Each story description MUST start with "repo: {default_repo}" on its own line
so the Coding Agent knows where to work.

Respond ONLY with valid JSON (no markdown fences):
{{
  "module_name": "<short epic name>",
  "module_description": "<one sentence describing the epic>",
  "stories": [
    {{
      "title": "<story title>",
      "description": "repo: {default_repo}\\n<2-3 sentences: what to implement and how>",
      "priority": "urgent" | "high" | "medium" | "low" | "none"
    }}
  ]
}}
Stories are implemented in the order listed.
"""


def _style_block(sdlc_directive: str, best_practices: str) -> str:
    parts = []
    if sdlc_directive:
        parts.append(
            "Development style — follow strictly when splitting and ORDERING stories:\n"
            + sdlc_directive
        )
    if best_practices:
        parts.append("Stack conventions to honor:\n" + best_practices)
    return ("\n" + "\n\n".join(parts) + "\n") if parts else ""


def decompose_idea(
    title: str,
    description: str,
    *,
    model: str,
    api_key: str = "",
    default_repo: str = "devadmin/sandbox",
    sdlc_directive: str = "",
    best_practices: str = "",
    stack: str = "",
    redis_conn=None,
) -> dict:
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": _PROMPT.format(
                    title=title,
                    description=description[:4000],
                    default_repo=default_repo,
                    style_block=_style_block(sdlc_directive, best_practices),
                ),
            },
        ],
        "temperature": 0.2,
    }
    if api_key:
        kwargs["api_key"] = api_key

    resp = litellm.completion(**kwargs)
    if redis_conn is not None:
        try:
            from reviewer.telemetry import record_usage
            record_usage(redis_conn, "planner", model, resp, stack=stack)
        except Exception:
            pass
    raw = resp.choices[0].message.content or ""
    return _parse(raw, title, default_repo)


def _parse(raw: str, title: str, default_repo: str) -> dict:
    clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(clean)
        if "module_name" in data and "stories" in data:
            return data
    except json.JSONDecodeError:
        log.warning("decomposer_json_error", raw=raw[:200])

    # Fallback: single story from the idea itself
    return {
        "module_name": title[:60],
        "module_description": f"Implement: {title}",
        "stories": [
            {
                "title": f"Implement {title}",
                "description": f"repo: {default_repo}\nImplement the feature as described in the idea.",
                "priority": "medium",
            }
        ],
    }
