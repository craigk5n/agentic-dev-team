"""
Prompt store — per-role LLM prompt templates, backed by Redis.

Single Redis key: "agent_prompts" → JSON dict { prompt_key: custom_value }.
When a key is absent the default defined here is used.

GET /api/prompts        → list all prompts with defaults + current values
PUT /api/prompts/{key}  → save custom value
DELETE /api/prompts/{key} → reset to default
"""

from __future__ import annotations
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis

_KEY = "agent_prompts"

# ---------------------------------------------------------------------------
# Default prompts
# ---------------------------------------------------------------------------

CODER_STORY = """\
You are a coding agent. Implement the following story right now. \
Do NOT ask for clarification — the story description is your complete specification. \
Do NOT say you are ready to help. Just implement it.

## Story: {story_title}

{story_description}

STEPS:
1. List the files in the repo to understand what exists.
2. Read the relevant existing files.
3. Implement the story by writing or modifying files. \
If an existing app is present, build on top of it. \
If the repo is empty or only has a README, create a minimal scaffold \
(Python + FastAPI by default) then implement the story.
4. Write real, working code — no placeholders or stubs unless the story explicitly asks for them.
5. Do not touch files unrelated to this story.
Start implementing now."""

CODER_REVIEW_FIX = """\
## Fix review feedback for: {story_title}

## Review comments to address
{feedback}

## Original story description
{story_description}

INSTRUCTIONS:
1. Read the existing code in the repo first.
2. Address ALL review comments listed above — that is the primary objective.
3. Make only the changes needed to satisfy the feedback.
4. Do not introduce unrelated changes."""

REVIEWER_SYSTEM = """\
You are a senior code reviewer. Be concise and specific. \
Only flag real issues — no style nitpicks unless they indicate a bug."""

REVIEWER_TASK = """\
Review the following git diff. Identify:
1. Bugs or logic errors
2. Security vulnerabilities (injection, secret exposure, SSRF, auth bypass)
3. Missing or incorrect error handling
4. Significant readability issues that mask bugs

Diff (may be truncated):
{diff}

Respond ONLY with valid JSON matching this schema exactly:
{{
  "status": "pass" | "warn" | "fail",
  "summary": "<one sentence>",
  "findings": [
    {{"severity": "critical"|"high"|"medium"|"low",
      "file": "<path>", "line": <int or null>,
      "message": "<actionable description>"}}
  ]
}}

Rules:
- "fail"  if any critical or high severity finding
- "warn"  if medium/low findings only
- "pass"  if no meaningful findings"""

TESTER_SUMMARIZE = """\
Summarise these test results in one sentence and list any failures.

Test output:
{output}

Respond ONLY with valid JSON:
{{
  "status": "pass" | "fail",
  "summary": "<one sentence>",
  "failures": ["<test name or error>", ...]
}}"""

# Ordered list of all prompt definitions for the UI
PROMPT_DEFS: list[dict] = [
    {
        "key": "coder.story",
        "label": "Coder — Story Implementation",
        "description": "Sent to opencode when a new story is claimed. "
                       "Use {story_title} and {story_description} as placeholders.",
        "default": CODER_STORY,
    },
    {
        "key": "coder.review_fix",
        "label": "Coder — Review Fix",
        "description": "Sent to opencode when addressing PR review comments. "
                       "Placeholders: {story_title}, {story_description}, {feedback}.",
        "default": CODER_REVIEW_FIX,
    },
    {
        "key": "reviewer.system",
        "label": "Reviewer — System Prompt",
        "description": "System-role message for the code review LLM.",
        "default": REVIEWER_SYSTEM,
    },
    {
        "key": "reviewer.task",
        "label": "Reviewer — Task Prompt",
        "description": "User-role message for code review. Use {diff} as the diff placeholder.",
        "default": REVIEWER_TASK,
    },
    {
        "key": "tester.summarize",
        "label": "Tester — Result Summarizer",
        "description": "Asks the LLM to parse raw test output into pass/fail JSON. "
                       "Use {output} as the placeholder.",
        "default": TESTER_SUMMARIZE,
    },
]

_DEFAULTS: dict[str, str] = {p["key"]: p["default"] for p in PROMPT_DEFS}


def _load(r: "redis.Redis") -> dict[str, str]:
    data = r.get(_KEY)
    if not data:
        return {}
    try:
        return json.loads(data)
    except Exception:
        return {}


def _save(r: "redis.Redis", store: dict[str, str]) -> None:
    r.set(_KEY, json.dumps(store))


def get_prompt(r: "redis.Redis", key: str) -> str:
    """Return the custom prompt if set, else the hardcoded default."""
    store = _load(r)
    return store.get(key, _DEFAULTS.get(key, ""))


def set_prompt(r: "redis.Redis", key: str, value: str) -> None:
    if key not in _DEFAULTS:
        raise ValueError(f"Unknown prompt key: {key!r}")
    store = _load(r)
    store[key] = value
    _save(r, store)


def delete_prompt(r: "redis.Redis", key: str) -> None:
    store = _load(r)
    store.pop(key, None)
    _save(r, store)


def list_prompts(r: "redis.Redis") -> list[dict]:
    """Return all prompt definitions with their current (possibly custom) values."""
    store = _load(r)
    return [
        {
            "key": p["key"],
            "label": p["label"],
            "description": p["description"],
            "default": p["default"],
            "current": store.get(p["key"], p["default"]),
            "is_custom": p["key"] in store,
        }
        for p in PROMPT_DEFS
    ]
