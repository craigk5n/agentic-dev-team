"""
Thin litellm wrapper with OpenRouter support.

Model string conventions:
  openrouter/anthropic/claude-sonnet-4-6  → routed through OpenRouter
  anthropic/claude-sonnet-4-6             → direct Anthropic API
  ollama/llama3                           → local Ollama

The caller passes the resolved api_key; this module stays stateless.
"""

from __future__ import annotations
import json
import re

import litellm
import structlog

log = structlog.get_logger()

# Truncate diffs to avoid blowing context budgets
_MAX_DIFF_CHARS = 24_000


def _supports_structured(model: str) -> bool:
    """Whether the model advertises structured-output support (from the shared model-meta
    cache). Guarded — safe no-op if the event_bus package / cache isn't available."""
    try:
        from reviewer.config import settings
        import redis as _redis
        from event_bus.models_catalog import supports_structured
        r = _redis.from_url(settings.redis_url, decode_responses=False)
        return bool(supports_structured(r, model))
    except Exception:
        return False


def complete(
    model: str,
    messages: list[dict],
    *,
    api_key: str = "",
    temperature: float = 0.0,
    telemetry_role: str = "",
    telemetry_stack: str = "",
    structured: bool = False,
) -> str:
    """
    Call any litellm-supported model and return the text response.

    Pass telemetry_role (e.g. "code_review", "test_run", "security") to record
    token usage and estimated cost to Redis. Failure to record is always silent.

    ``structured=True`` (for callers that require JSON) sends response_format when the
    model supports it, so a capable model can't answer with prose — attacking the
    bad-JSON verdict failures ("please paste the diff", empty replies) on flaky models.
    """
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if structured and _supports_structured(model):
        kwargs["response_format"] = {"type": "json_object"}
    resp = litellm.completion(**kwargs)

    if telemetry_role:
        try:
            from reviewer.config import settings
            import redis as _redis
            from reviewer.telemetry import record_usage
            r = _redis.from_url(settings.redis_url, decode_responses=False)
            record_usage(r, telemetry_role, model, resp, stack=telemetry_stack)
        except Exception as exc:
            log.debug("telemetry_skipped", role=telemetry_role, error=str(exc))

    return resp.choices[0].message.content or ""


_REVIEW_SYSTEM = """\
You are a senior code reviewer. Be concise and specific. \
Only flag real issues — no style nitpicks unless they indicate a bug."""

_REVIEW_PROMPT = """\
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
- "pass"  if no meaningful findings
"""


def review_diff(
    diff: str,
    *,
    model: str,
    api_key: str = "",
    system_prompt: str = "",
    task_prompt: str = "",
    stack: str = "",
) -> dict:
    """Call the LLM to review a git diff. Returns a structured verdict dict."""
    truncated = diff[:_MAX_DIFF_CHARS]
    system = system_prompt or _REVIEW_SYSTEM
    task = task_prompt or _REVIEW_PROMPT
    try:
        user_content = task.format(diff=truncated)
    except (KeyError, ValueError):
        user_content = task
    raw = complete(
        model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        api_key=api_key,
        telemetry_role="reviewer",
        telemetry_stack=stack,
        structured=True,
    )
    return _parse_json_response(raw, role="code_review")


_SUMMARISE_PROMPT = """\
Summarise these test results in one sentence and list any failures.

Test output:
{output}

Respond ONLY with valid JSON:
{{
  "status": "pass" | "fail",
  "summary": "<one sentence>",
  "failures": ["<test name or error>", ...]
}}
"""


def summarise_test_output(
    output: str,
    *,
    model: str,
    api_key: str = "",
    task_prompt: str = "",
    stack: str = "",
) -> dict:
    """Ask the LLM to parse raw test output into a structured verdict."""
    truncated = output[:_MAX_DIFF_CHARS]
    template = task_prompt or _SUMMARISE_PROMPT
    try:
        content = template.format(output=truncated)
    except (KeyError, ValueError):
        content = template
    raw = complete(
        model,
        [{"role": "user", "content": content}],
        api_key=api_key,
        telemetry_role="tester",
        telemetry_stack=stack,
        structured=True,
    )
    return _parse_json_response(raw, role="test_run")


def _parse_json_response(raw: str, *, role: str) -> dict:
    """Extract a JSON object from an LLM response, tolerating markdown fences."""
    # Strip ```json ... ``` fences if present
    clean = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
    clean = re.sub(r"\s*```$", "", clean.strip(), flags=re.MULTILINE)
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        log.warning("llm_json_parse_error", role=role, raw=raw[:200])
        return {"status": "warn", "summary": "LLM response was not valid JSON", "findings": []}
