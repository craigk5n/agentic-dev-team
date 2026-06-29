"""
Daily LLM spend guard.

Sums today's recorded LLM cost (from reviewer.telemetry) and reports whether a
configured daily cap has been reached. When it has, dispatch points pause new
agent work — a cost backstop that protects the bill even from authorized
overuse or a runaway agent loop. The board auth keeps strangers out; this keeps
spend bounded from the inside.

The cap lives in LimitsConfig.max_cost_usd_daily (0 = unlimited).
"""

from __future__ import annotations
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import redis

log = structlog.get_logger()


def today_spend(r: "redis.Redis") -> float:
    """Total LLM cost in USD recorded for today (UTC), across all roles."""
    try:
        from reviewer.telemetry import read_all
        return round(sum(rec.get("cost_usd", 0.0) for rec in read_all(r, days=1)), 6)
    except Exception as exc:
        log.warning("cost_spend_read_failed", error=str(exc))
        return 0.0


def over_budget(r: "redis.Redis", max_daily_usd: float) -> bool:
    """True when a positive daily cap is set and today's spend has reached it."""
    if not max_daily_usd or max_daily_usd <= 0:
        return False
    spent = today_spend(r)
    if spent >= max_daily_usd:
        log.warning("cost_budget_exceeded", spent_usd=spent, cap_usd=max_daily_usd)
        return True
    return False
