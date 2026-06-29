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
) -> None:
    """
    Record token usage and estimated USD cost from a litellm completion response.
    Safe to call with any response object — silently skips if data is missing.
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

        # UTC to match read_all() (time.gmtime); otherwise the write/read keys
        # diverge once local time crosses the UTC day boundary.
        date = time.strftime("%Y-%m-%d", time.gmtime())
        key = f"telemetry:llm:{date}"
        prefix = f"{role}:{model}"

        pipe = r.pipeline()
        pipe.hincrbyfloat(key, f"{prefix}:cost_usd", cost)
        pipe.hincrby(key, f"{prefix}:input_tokens", int(getattr(usage, "prompt_tokens", 0) or 0))
        pipe.hincrby(key, f"{prefix}:output_tokens", int(getattr(usage, "completion_tokens", 0) or 0))
        pipe.hincrby(key, f"{prefix}:calls", 1)
        pipe.expire(key, _TTL_SECONDS)
        pipe.execute()

        log.debug("telemetry_recorded", role=role, model=model, cost_usd=round(cost, 6))
    except Exception as exc:
        log.warning("telemetry_write_failed", role=role, model=model, error=str(exc))


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
