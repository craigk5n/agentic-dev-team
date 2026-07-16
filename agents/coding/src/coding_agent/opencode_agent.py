"""
opencode-based agentic coding loop.

Runs `opencode run --dir <repo> --model <model> <prompt>` as a subprocess,
streams output line-by-line via an optional callback, and returns a summary string.
opencode handles all file reads/writes/commands inside the repo directory.
"""

from __future__ import annotations
import json as _json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable

import structlog

log = structlog.get_logger()

DEFAULT_TIMEOUT = 900  # 15 minutes per story

_ANSI_ESCAPE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# HS-9: opencode's `run` output sometimes carries a usage/cost footer. Parse it when present
# so per-story coder cost is REAL rather than a chars/4 estimate. Best-effort — patterns are
# permissive and absence just falls back to the estimate.
_COST_RE = re.compile(r"(?:cost|spent|total)[^\n$]*\$\s*([0-9]+(?:\.[0-9]+)?)", re.I)
_IN_TOK_RE = re.compile(r"([0-9][0-9,]*)\s*(?:input|prompt)\s*tokens?", re.I)
_OUT_TOK_RE = re.compile(r"([0-9][0-9,]*)\s*(?:output|completion)\s*tokens?", re.I)


def parse_opencode_usage(text: str) -> dict:
    """Extract real token/cost usage from opencode output, or ``{}`` if none is present.

    Returns any of {"input_tokens", "output_tokens", "cost_usd"} that could be parsed. The
    caller prefers these real numbers over the chars/4 estimate when available."""
    out: dict = {}
    if not text:
        return out
    m = _IN_TOK_RE.search(text)
    if m:
        out["input_tokens"] = int(m.group(1).replace(",", ""))
    m = _OUT_TOK_RE.search(text)
    if m:
        out["output_tokens"] = int(m.group(1).replace(",", ""))
    m = _COST_RE.search(text)
    if m:
        out["cost_usd"] = float(m.group(1))
    return out


def parse_opencode_json_usage(text: str) -> dict:
    """Sum REAL token/cost usage across every assistant message in an
    ``opencode run --format json`` event stream (JSONL) — this captures all internal
    tool-call turns, not just the final message, killing the ~3x undercount.

    Schema (from @opencode-ai/sdk, verified): events ``{type:"message.updated",
    properties:{info: AssistantMessage}}`` where AssistantMessage carries
    ``cost: number`` and ``tokens:{input,output,reasoning,cache:{read,write}}``.
    Streaming emits the same message id repeatedly, so we keep the latest state per id
    then sum. Returns {} if the stream carries no assistant usage."""
    if not text:
        return {}
    msgs: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            evt = _json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(evt, dict) or evt.get("type") != "message.updated":
            continue
        info = (evt.get("properties") or {}).get("info") or {}
        if info.get("role") != "assistant" or not info.get("id"):
            continue
        toks = info.get("tokens") or {}
        msgs[info["id"]] = {  # latest state wins
            "cost": float(info.get("cost") or 0.0),
            "input": int(toks.get("input") or 0),
            "output": int(toks.get("output") or 0),
            "reasoning": int(toks.get("reasoning") or 0),
            "model": info.get("modelID") or "",
        }
    if not msgs:
        return {}
    model = next((m["model"] for m in reversed(list(msgs.values())) if m["model"]), "")
    return {
        "input_tokens": sum(m["input"] for m in msgs.values()),
        "output_tokens": sum(m["output"] for m in msgs.values()),
        "reasoning_tokens": sum(m["reasoning"] for m in msgs.values()),
        "cost_usd": sum(m["cost"] for m in msgs.values()),
        "model": model,
    }


def extract_opencode_json_summary(text: str) -> str:
    """Join assistant-authored text parts from an opencode JSON event stream into a
    human-readable summary (the JSON stream is not itself readable). Excludes user and
    synthetic parts. Returns '' if none found."""
    if not text:
        return ""
    assistant_ids: set[str] = set()
    parts: dict[str, str] = {}       # part id -> latest text
    part_msg: dict[str, str] = {}    # part id -> owning message id
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] != "{":
            continue
        try:
            evt = _json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(evt, dict):
            continue
        props = evt.get("properties") or {}
        if evt.get("type") == "message.updated":
            info = props.get("info") or {}
            if info.get("role") == "assistant" and info.get("id"):
                assistant_ids.add(info["id"])
        elif evt.get("type") == "message.part.updated":
            part = props.get("part") or {}
            if part.get("type") == "text" and part.get("id") and not part.get("synthetic"):
                parts[part["id"]] = part.get("text") or ""
                part_msg[part["id"]] = part.get("messageID") or ""
    ordered = [txt for pid, txt in parts.items()
               if part_msg.get(pid) in assistant_ids and txt.strip()]
    return "\n".join(ordered).strip()

# Phrases OpenRouter/providers emit when an LLM call fails (rate limit, provider outage,
# no available endpoint). Specific enough not to collide with normal code the agent writes.
_PROVIDER_ERROR_MARKERS = (
    "temporarily rate-limited",
    "rate-limited upstream",
    "provider returned error",
    "no endpoints found",
    "no allowed providers",
    "insufficient credits",
    "quota exceeded",
    "overloaded_error",
    "503 service unavailable",
)


def _looks_like_provider_error(output: str) -> bool:
    low = (output or "").lower()
    return any(mk in low for mk in _PROVIDER_ERROR_MARKERS)


# opencode surfaces an unknown/misconfigured model as ProviderModelNotFoundError and can
# still exit 0. That's a CONFIG error, not transient — retrying the same model is futile;
# the caller should fall back to a known-good model instead of silently committing "no
# changes". (Common cause: an escalate/override model named in litellm style that isn't
# in opencode's provider registry.)
_MODEL_NOT_FOUND_MARKERS = (
    "providermodelnotfounderror",
    "model not found",
    "modelnotfounderror",
    "no such model",
    "unknown model",
    "invalid model",
)


class ModelNotFoundError(RuntimeError):
    """opencode could not resolve the requested model (a config error, not transient)."""


def _looks_like_model_not_found(output: str) -> bool:
    low = (output or "").lower()
    return any(mk in low for mk in _MODEL_NOT_FOUND_MARKERS)


# A provider "out of credit / quota" rejection is unrecoverable — retrying can't fix an
# empty balance. Surface it distinctly (a subset of the generic provider errors above) so
# the caller parks the story for the operator instead of burning the retry cap.
# NOTE: kept byte-identical across the decoupled coding/planner/event-bus packages (each
# runs standalone in its own sandbox and cannot share an import) — keep the copies in sync.
# "insufficient credit" (singular) is deliberate: as a substring it also matches the plural.
_CREDIT_ERROR_MARKERS = (
    "insufficient credit", "requires more credits", "add more credits",
    "quota exceeded", "payment required", "insufficient_quota",
)


class InsufficientCreditsError(RuntimeError):
    """The model provider rejected the call for lack of credit/quota (operator action)."""


def _looks_like_credit_error(output: str) -> bool:
    low = (output or "").lower()
    return any(mk in low for mk in _CREDIT_ERROR_MARKERS)


def _clean(line: str) -> str:
    return _ANSI_ESCAPE.sub("", line).rstrip("\r")


def run_opencode_agent(
    story_title: str,
    story_description: str,
    repo_dir: str,
    model: str,
    openrouter_api_key: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    review_comments: list[dict] | None = None,
    prompt_template: str = "",
    log_line: Callable[[str], None] | None = None,
    fallback_model: str = "",
    usage_source: str = "text",
    usage_out: dict | None = None,
) -> str:
    """
    Run opencode non-interactively on the given repo and story.
    Returns a summary of what was implemented.
    Raises RuntimeError on failure.

    ``fallback_model`` — if the requested model can't be resolved by opencode
    (ModelNotFoundError), transparently retry once with this known-good model instead of
    failing. Lets an unavailable escalate/override model degrade gracefully to the base
    coder model rather than wedging the story.

    ``usage_source`` — "text" (default, proven path: human-formatted output) or "json"
    (runs with ``--format json`` and captures REAL per-turn token/cost usage). When a
    dict is passed as ``usage_out`` it is populated in place with the model actually used
    (``model_used``, fallback-aware) and, in json mode, accumulated real usage
    ({input_tokens, output_tokens, reasoning_tokens, cost_usd, source:"json"}).
    """
    opencode_bin = shutil.which("opencode")
    if not opencode_bin:
        raise RuntimeError(
            "opencode not found in PATH. "
            "Mount the opencode binary into the container or install it."
        )

    if review_comments:
        feedback = "\n".join(
            f"- [{c.get('path', 'general')}] {c.get('body', '').strip()}"
            for c in review_comments if c.get("body", "").strip()
        )
        _DEFAULT_FIX = (
            "## Fix review feedback for: {story_title}\n\n"
            "## Review comments to address\n{feedback}\n\n"
            "## Original story description\n{story_description}\n\n"
            "INSTRUCTIONS:\n"
            "1. Read the existing code in the repo first.\n"
            "2. Address ALL review comments listed above — that is the primary objective.\n"
            "3. Make only the changes needed to satisfy the feedback.\n"
            "4. Do not introduce unrelated changes."
        )
        template = prompt_template or _DEFAULT_FIX
        try:
            prompt = template.format(
                story_title=story_title,
                story_description=story_description or "(no description)",
                feedback=feedback,
            )
        except (KeyError, ValueError):
            prompt = template
    else:
        _DEFAULT_STORY = (
            "You are a coding agent. Implement the following story right now. "
            "Do NOT ask for clarification — the story description is your complete specification. "
            "Do NOT say you are ready to help. Just implement it.\n\n"
            "## Story: {story_title}\n\n"
            "{story_description}\n\n"
            "STEPS:\n"
            "1. List the files in the repo to understand what exists.\n"
            "2. Read the relevant existing files.\n"
            "3. Implement the story by writing or modifying files. "
            "If an existing app is present, build on top of it. "
            "If the repo is empty or only has a README, create a minimal scaffold "
            "(Python + FastAPI by default) then implement the story.\n"
            "4. Write real, working code — no placeholders or stubs unless the story explicitly asks for them.\n"
            "5. Do not touch files unrelated to this story.\n"
            "Start implementing now."
        )
        template = prompt_template or _DEFAULT_STORY
        try:
            prompt = template.format(
                story_title=story_title,
                story_description=story_description or "(no description)",
            )
        except (KeyError, ValueError):
            prompt = template

    env = {**os.environ}
    if openrouter_api_key:
        env["OPENROUTER_API_KEY"] = openrouter_api_key

    def _invoke(use_model: str) -> str:
        cmd = [opencode_bin, "run", "--dir", repo_dir, "--model", use_model]
        if usage_source == "json":
            cmd += ["--format", "json"]
        cmd.append(prompt)
        log.info("opencode_start", model=use_model, repo=repo_dir, story=story_title)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr so caller sees everything
            text=True,
            env=env,
        )
        collected: list[str] = []

        def _reader() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = _clean(raw)
                collected.append(line)
                if log_line:
                    try:
                        log_line(line)
                    except Exception:
                        pass

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            reader.join(timeout=5)
            raise RuntimeError(f"opencode timed out after {timeout}s")
        reader.join(timeout=5)
        output = "\n".join(collected)
        if output:
            log.info("opencode_stdout", chars=len(output))

        # A model opencode can't resolve is a config error (it may even exit 0). Surface
        # it distinctly so the caller can fall back rather than commit "no changes".
        if _looks_like_model_not_found(output):
            raise ModelNotFoundError(f"opencode could not resolve model {use_model!r}: {output[-300:]}")
        if proc.returncode != 0:
            raise RuntimeError(f"opencode exited {proc.returncode}: {output[-400:]}")
        # opencode can exit 0 even when the LLM call itself failed (e.g. a free-tier 429
        # surfaced as a provider error). Such a run produced NO real work — treat it as an
        # error so the story is retried, not silently committed as "no changes / done".
        if _looks_like_credit_error(output):
            raise InsufficientCreditsError(
                f"opencode LLM call rejected for insufficient credit/quota: {output[-300:]}")
        if _looks_like_provider_error(output):
            raise RuntimeError(f"opencode LLM call failed (provider error/rate-limit): {output[-300:]}")

        # Record which model actually ran (fallback-aware) + real usage in json mode.
        # Accumulates across calls so TDD-retry invocations sum into one story total.
        if usage_out is not None:
            usage_out["model_used"] = use_model
            if usage_source == "json":
                u = parse_opencode_json_usage(output)
                if u:
                    for k in ("input_tokens", "output_tokens", "reasoning_tokens"):
                        usage_out[k] = usage_out.get(k, 0) + u[k]
                    usage_out["cost_usd"] = usage_out.get("cost_usd", 0.0) + u["cost_usd"]
                    usage_out["source"] = "json"

        if usage_source == "json":
            summary = extract_opencode_json_summary(output) or "Implementation complete"
        else:
            summary = output.strip() or "Implementation complete"
        log.info("opencode_done", story=story_title, model=use_model)
        return summary

    try:
        return _invoke(model)
    except ModelNotFoundError:
        if fallback_model and fallback_model != model:
            log.warning("opencode_model_fallback", requested=model, fallback=fallback_model)
            if log_line:
                log_line(f"[coder] model '{model}' not available in opencode — "
                         f"falling back to '{fallback_model}'")
            return _invoke(fallback_model)
        raise
