"""
Per-(role, model) outcome counters — the success/quality half of model metrics.

`telemetry:llm:{date}` already tracks *volume* (calls, cost, tokens) per role+model.
This module tracks *outcomes* so the Metrics tab can show success rate, first-pass
rate, and escalation rate per model — the signals that actually decide which model to
use. Counters are written at the points where an outcome becomes known:

  coder verdicts:   merged | abandoned | escalated | first_pass   (attributed to the
                    coder model that built the story)
  review verdicts:  pass | fail | error                          (per reviewer/tester/
                    security model)

Same daily-hash + 30-day-TTL shape as reviewer.telemetry, keyed by UTC date so writes
and reads agree across the local-midnight boundary. All writes are best-effort — a
telemetry failure must never break the pipeline.
"""
from __future__ import annotations
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis

_TTL = 30 * 86_400
CODER_OUTCOMES = ("merged", "abandoned", "escalated", "first_pass")
# 'flip'  = a re-review changed this model's verdict (instability / non-convergence)
# 'rate_limited' = the call failed on a provider 429 (free-tier reliability)
VERDICT_OUTCOMES = ("pass", "fail", "error", "flip", "rate_limited")


def _key(date: str | None = None) -> str:
    date = date or time.strftime("%Y-%m-%d", time.gmtime())
    return f"telemetry:outcome:{date}"


def record_outcome(r: "redis.Redis", role: str, model: str, outcome: str, n: int = 1) -> None:
    """Increment the counter for one (role, model, outcome). Best-effort."""
    if r is None or not model or not outcome:
        return
    try:
        key = _key()
        pipe = r.pipeline()
        pipe.hincrby(key, f"{role}:{model}:{outcome}", n)
        pipe.expire(key, _TTL)
        pipe.execute()
    except Exception:
        pass


def record_latency(r: "redis.Redis", role: str, model: str, ms: float) -> None:
    """Accumulate call latency for a (role, model) so the Metrics tab can show avg
    duration per model. Best-effort."""
    if r is None or not model or ms is None or ms < 0:
        return
    try:
        key = f"telemetry:latency:{time.strftime('%Y-%m-%d', time.gmtime())}"
        pipe = r.pipeline()
        pipe.hincrbyfloat(key, f"{role}:{model}:ms_sum", float(ms))
        pipe.hincrby(key, f"{role}:{model}:count", 1)
        pipe.expire(key, _TTL)
        pipe.execute()
    except Exception:
        pass


def read_latency(r: "redis.Redis", days: int = 30) -> dict:
    """Aggregate latency → {(role, model): avg_ms}."""
    sums: dict = {}
    if r is None:
        return {}
    try:
        now = time.time()
        for i in range(days):
            date = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86_400))
            h = r.hgetall(f"telemetry:latency:{date}")
            for field, val in h.items():
                field = field.decode() if isinstance(field, bytes) else field
                if field.endswith(":ms_sum"):
                    rm = field[:-len(":ms_sum")]
                    sums.setdefault(rm, [0.0, 0])[0] += float(val)
                elif field.endswith(":count"):
                    rm = field[:-len(":count")]
                    sums.setdefault(rm, [0.0, 0])[1] += int(val)
    except Exception:
        return {}
    out = {}
    for rm, (ms, n) in sums.items():
        parts = rm.split(":")
        role, model = parts[0], ":".join(parts[1:])
        if n:
            out[(role, model)] = round(ms / n)
    return out


def read_outcomes(r: "redis.Redis", days: int = 30) -> dict:
    """Aggregate outcome counters over the last `days` → {(role, model): {outcome: count}}."""
    out: dict = {}
    if r is None:
        return out
    try:
        now = time.time()
        for i in range(days):
            date = time.strftime("%Y-%m-%d", time.gmtime(now - i * 86_400))
            h = r.hgetall(_key(date))
            for field, val in h.items():
                field = field.decode() if isinstance(field, bytes) else field
                parts = field.split(":")
                if len(parts) < 3:
                    continue
                outcome = parts[-1]
                role = parts[0]
                model = ":".join(parts[1:-1])  # model ids can contain ':'
                bucket = out.setdefault((role, model), {})
                bucket[outcome] = bucket.get(outcome, 0) + int(val)
    except Exception:
        pass
    return out
