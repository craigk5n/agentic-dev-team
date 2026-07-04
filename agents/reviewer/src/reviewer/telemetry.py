"""
Cost and token telemetry for LLM calls.

Writes to Redis hashes keyed by date:
  telemetry:llm:{YYYY-MM-DD}  →  hash fields {role}:{model}:{metric}

Metrics recorded per (role, model, day): cost_usd, input_tokens, output_tokens, calls
Retention: 30 days (Redis TTL).

All operations are best-effort — failures are logged but never raised.
"""

from __future__ import annotations
import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import redis

log = structlog.get_logger()

_TTL_SECONDS = 30 * 86_400  # 30 days


def record_usage(
    r: "redis.Redis",
    role: str,
    model: str,
    response,
    stack: str = "",
    project: str = "",
) -> None:
    """
    Record token usage and estimated USD cost from a litellm completion response.
    Safe to call with any response object — silently skips if data is missing.

    When `stack` is given, the same metrics are also accumulated in a per-stack
    hash (telemetry:stack:{date}). When `project` (a project/idea id) is given, they
    are also accumulated per-project (telemetry:project:{date}) so total spend can be
    attributed to a project.
    """
    try:
        usage = getattr(response, "usage", None)
        if not usage:
            return

        # Prefer OpenRouter's upstream cost field (already computed, model-agnostic)
        cost = float(getattr(usage, "cost", None) or 0.0)
        if cost == 0.0:
            try:
                import litellm
                cost = litellm.completion_cost(completion_response=response)
            except Exception:
                cost = 0.0

        in_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(usage, "completion_tokens", 0) or 0)

        # UTC to match read_all() (time.gmtime); otherwise the write/read keys
        # diverge once local time crosses the UTC day boundary.
        date = time.strftime("%Y-%m-%d", time.gmtime())
        key = f"telemetry:llm:{date}"
        prefix = f"{role}:{model}"

        pipe = r.pipeline()
        pipe.hincrbyfloat(key, f"{prefix}:cost_usd", cost)
        pipe.hincrby(key, f"{prefix}:input_tokens", in_tok)
        pipe.hincrby(key, f"{prefix}:output_tokens", out_tok)
        pipe.hincrby(key, f"{prefix}:calls", 1)
        pipe.expire(key, _TTL_SECONDS)

        if stack:
            skey = f"telemetry:stack:{date}"
            pipe.hincrbyfloat(skey, f"{stack}:cost_usd", cost)
            pipe.hincrby(skey, f"{stack}:input_tokens", in_tok)
            pipe.hincrby(skey, f"{stack}:output_tokens", out_tok)
            pipe.hincrby(skey, f"{stack}:calls", 1)
            pipe.expire(skey, _TTL_SECONDS)

        if project:
            # project ids are UUIDs (no ':'), so the {project}:{metric} field parses cleanly.
            pkey = f"telemetry:project:{date}"
            pipe.hincrbyfloat(pkey, f"{project}:cost_usd", cost)
            pipe.hincrby(pkey, f"{project}:input_tokens", in_tok)
            pipe.hincrby(pkey, f"{project}:output_tokens", out_tok)
            pipe.hincrby(pkey, f"{project}:calls", 1)
            pipe.expire(pkey, _TTL_SECONDS)

        pipe.execute()

        log.debug("telemetry_recorded", role=role, model=model, stack=stack,
                  project=project, cost_usd=round(cost, 6))
    except Exception as exc:
        log.warning("telemetry_write_failed", role=role, model=model, error=str(exc))


def read_stack_usage(r: "redis.Redis", days: int = 30) -> list[dict]:
    """
    Return per-stack cost/usage totals summed over the last `days` days.
    Each entry: {stack, cost_usd, input_tokens, output_tokens, calls}
    """
    totals: dict[str, dict] = {}
    today = time.time()
    for offset in range(days):
        date = time.strftime("%Y-%m-%d", time.gmtime(today - offset * 86_400))
        try:
            raw = r.hgetall(f"telemetry:stack:{date}")
        except Exception:
            continue
        for k, v in (raw or {}).items():
            field = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            parts = field.rsplit(":", 1)
            if len(parts) != 2:
                continue
            stack, metric = parts
            entry = totals.setdefault(stack, {
                "stack": stack, "cost_usd": 0.0,
                "input_tokens": 0, "output_tokens": 0, "calls": 0})
            if metric == "cost_usd":
                entry["cost_usd"] += float(val)
            elif metric in ("input_tokens", "output_tokens", "calls"):
                entry[metric] += int(val)
    return sorted(totals.values(), key=lambda x: x["cost_usd"], reverse=True)


def read_project_usage(r: "redis.Redis", days: int = 30) -> list[dict]:
    """
    Return per-project cost/usage totals summed over the last `days` days.
    Each entry: {project, cost_usd, input_tokens, output_tokens, calls} where `project`
    is the project/idea id (the caller resolves it to a display title).
    """
    totals: dict[str, dict] = {}
    today = time.time()
    for offset in range(days):
        date = time.strftime("%Y-%m-%d", time.gmtime(today - offset * 86_400))
        try:
            raw = r.hgetall(f"telemetry:project:{date}")
        except Exception:
            continue
        for k, v in (raw or {}).items():
            field = k.decode() if isinstance(k, bytes) else k
            val = v.decode() if isinstance(v, bytes) else v
            parts = field.rsplit(":", 1)
            if len(parts) != 2:
                continue
            project, metric = parts
            entry = totals.setdefault(project, {
                "project": project, "cost_usd": 0.0,
                "input_tokens": 0, "output_tokens": 0, "calls": 0})
            if metric == "cost_usd":
                entry["cost_usd"] += float(val)
            elif metric in ("input_tokens", "output_tokens", "calls"):
                entry[metric] += int(val)
    return sorted(totals.values(), key=lambda x: x["cost_usd"], reverse=True)


def read_all(r: "redis.Redis", days: int = 30) -> list[dict]:
    """
    Return a list of daily cost/usage records for the last `days` days.
    Each entry: {date, role, model, cost_usd, input_tokens, output_tokens, calls}
    """
    results = []
    today = time.time()
    for offset in range(days):
        date = time.strftime("%Y-%m-%d", time.gmtime(today - offset * 86_400))
        key = f"telemetry:llm:{date}"
        try:
            raw = r.hgetall(key)
        except Exception:
            continue
        if not raw:
            continue
        # Decode bytes keys/values
        data: dict[str, str] = {}
        for k, v in raw.items():
            dk = k.decode() if isinstance(k, bytes) else k
            dv = v.decode() if isinstance(v, bytes) else v
            data[dk] = dv

        # Group by role:model
        groups: dict[str, dict] = {}
        for field, val in data.items():
            parts = field.rsplit(":", 1)
            if len(parts) != 2:
                continue
            role_model, metric = parts[0], parts[1]
            if role_model not in groups:
                groups[role_model] = {"date": date, "cost_usd": 0.0,
                                       "input_tokens": 0, "output_tokens": 0, "calls": 0}
                rm_parts = role_model.split(":", 1)
                groups[role_model]["role"] = rm_parts[0]
                groups[role_model]["model"] = rm_parts[1] if len(rm_parts) > 1 else ""
            if metric == "cost_usd":
                groups[role_model]["cost_usd"] += float(val)
            elif metric in ("input_tokens", "output_tokens", "calls"):
                groups[role_model][metric] += int(val)

        results.extend(groups.values())

    return sorted(results, key=lambda x: (x["date"], x["role"], x["model"]))
