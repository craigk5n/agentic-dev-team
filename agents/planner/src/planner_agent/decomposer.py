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

Create:
1. One module (epic) grouping all stories
2. Between 3 and 7 stories — each a concrete, implementable task

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
"""


def decompose_idea(
    title: str,
    description: str,
    *,
    model: str,
    api_key: str = "",
    default_repo: str = "devadmin/sandbox",
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
                ),
            },
        ],
        "temperature": 0.2,
    }
    if api_key:
        kwargs["api_key"] = api_key

    resp = litellm.completion(**kwargs)
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
