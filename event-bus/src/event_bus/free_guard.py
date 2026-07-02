"""
OpenRouter free-tier request guard.

OpenRouter caps ``:free`` model usage at a fixed number of requests per day (50/day
for accounts with < $10 lifetime credit, 1000/day for >= $10). Those calls cost $0, so
the dollar cost cap (cost_guard) never catches them — a long run can silently exhaust
the free quota, after which reviewer/tester/security calls 429 and PRs stall.

This guard counts today's successful ``:free`` calls straight from the existing
telemetry (no extra write path) and reports how close you are to the daily limit, so
the board can warn before the wall — and optionally hold new work at the cap.

The limit lives in LimitsConfig.max_free_requests_daily (0 = unlimited / disabled).
"""

from __future__ import annotations
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import redis

log = structlog.get_logger()

_WARN_FRACTION = 0.8   # banner turns on at 80% of the daily limit


def free_calls_today(r: "redis.Redis") -> int:
    """Count today's (UTC) successful OpenRouter free-model calls across all roles."""
    try:
        from reviewer.telemetry import read_all
        return sum(int(rec.get("calls", 0)) for rec in read_all(r, days=1)
                   if ":free" in (rec.get("model") or ""))
    except Exception as exc:
        log.warning("free_calls_read_failed", error=str(exc))
        return 0


def free_quota_status(r: "redis.Redis", limit: int) -> dict:
    """Report free-tier usage vs the configured daily limit.

    Returns {used, limit, remaining, pct, warn, exceeded}. When the limit is 0/unset the
    guard is disabled (warn/exceeded always False) but usage is still reported.
    """
    used = free_calls_today(r)
    lim = int(limit) if limit and limit > 0 else 0
    remaining = max(lim - used, 0) if lim else None
    pct = round(100.0 * used / lim, 1) if lim else 0.0
    return {
        "used": used,
        "limit": lim,
        "remaining": remaining,
        "pct": pct,
        "warn": bool(lim) and used >= lim * _WARN_FRACTION,
        "exceeded": bool(lim) and used >= lim,
    }


def free_quota_exceeded(r: "redis.Redis", limit: int) -> bool:
    """True when a positive daily free-request limit is set and reached — used to hold
    new work so the pipeline degrades cleanly instead of erroring on 429s."""
    if not limit or limit <= 0:
        return False
    if free_calls_today(r) >= limit:
        log.warning("free_quota_exceeded", used=free_calls_today(r), limit=limit)
        return True
    return False
