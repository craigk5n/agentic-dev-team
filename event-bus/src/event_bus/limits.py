"""
Redis-backed rate and concurrency limits for agent job roles.

Rate limit:   sliding 1-minute window — checked before enqueuing a job.
Concurrency:  atomic INCR/DECR — checked and held during job execution.

Both limits come from LimitsConfig (runtime config key "runtime_config").
A limit value of 0 means unlimited.

Usage in handlers (rate check before enqueue):
    if not check_rate(r, "reviewer", config.limits.max_rpm_reviewer):
        raise HTTPException(429, "rate limit exceeded")

Usage in job functions (concurrency around execution):
    if not acquire_slot(r, "reviewer", config.limits.max_concurrent_reviewer):
        return {"status": "error", "reason": "concurrency_limit_exceeded"}
    try:
        result = do_work()
    finally:
        release_slot(r, "reviewer")
"""

from __future__ import annotations
import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import redis

log = structlog.get_logger()

_CONCURRENCY_KEY = "limits:concurrency:{role}"
_RATE_KEY = "limits:rate:{role}:{window}"
_REJECTED_KEY = "limits:rejected:{role}"


def acquire_slot(r: "redis.Redis", role: str, max_concurrent: int) -> bool:
    """
    Atomically claim a concurrency slot for `role`. Returns True if successful.

    Caller MUST call release_slot() when done (typically in a try/finally block).
    A 2-hour safety TTL prevents leaked slots from starving the system indefinitely.
    """
    if max_concurrent <= 0:
        return True

    key = _CONCURRENCY_KEY.format(role=role)
    count = r.incr(key)
    r.expire(key, 7200)  # 2h safety TTL

    if count > max_concurrent:
        r.decr(key)
        log.warning("concurrency_limit_hit", role=role, active=count - 1, max=max_concurrent)
        r.incr(_REJECTED_KEY.format(role=role))
        return False

    return True


def release_slot(r: "redis.Redis", role: str) -> None:
    """Decrement the concurrency counter. Guards against underflow."""
    key = _CONCURRENCY_KEY.format(role=role)
    val = r.decr(key)
    if val < 0:
        r.set(key, 0)


# Roles that acquire a concurrency slot during job execution (in the RQ worker).
_SLOT_ROLES = ("reviewer", "tester", "security")


def reconcile_slots(r: "redis.Redis") -> dict:
    """Reset every per-role concurrency counter to 0 — call this on WORKER startup.

    A worker killed mid-job (crash / --force-recreate) can't run its jobs' release_slot()
    finally blocks, so a slot acquired before the restart stays consumed for the key's
    2h TTL and starves that role (every new job returns 'concurrency_limit_exceeded'
    without doing any work). A fresh worker has zero in-flight jobs, so the truthful value
    is 0. Mirrors the coder in-flight reconciliation on the web side.
    """
    leaked: dict = {}
    for role in _SLOT_ROLES:
        key = _CONCURRENCY_KEY.format(role=role)
        try:
            prev = int(r.get(key) or 0)
        except (TypeError, ValueError):
            prev = 0
        if prev:
            leaked[role] = prev
        r.set(key, 0)
    if leaked:
        log.warning("concurrency_slots_reconciled", leaked=leaked)
    return leaked


def check_rate(r: "redis.Redis", role: str, max_per_minute: int) -> bool:
    """
    Sliding 1-minute window rate limit for `role`.
    Returns True if the call is within the limit.

    Uses a per-minute Redis counter with a 2-minute expiry. A new counter is
    created each minute, so bursting near the boundary is possible — acceptable
    for this use case.
    """
    if max_per_minute <= 0:
        return True

    window = int(time.time() // 60)
    key = _RATE_KEY.format(role=role, window=window)
    count = r.incr(key)
    r.expire(key, 120)  # keep for 2 minutes so the current window is always readable

    if count > max_per_minute:
        log.warning("rate_limit_hit", role=role, count=count, max_per_minute=max_per_minute)
        r.incr(_REJECTED_KEY.format(role=role))
        return False

    return True


def get_concurrency(r: "redis.Redis", role: str) -> int:
    """Current number of in-flight jobs for `role`."""
    val = r.get(_CONCURRENCY_KEY.format(role=role))
    return max(0, int(val)) if val else 0


def get_rejected(r: "redis.Redis", role: str) -> int:
    """Total rate/concurrency rejections since Redis last restarted."""
    val = r.get(_REJECTED_KEY.format(role=role))
    return int(val) if val else 0


def get_rate_window_count(r: "redis.Redis", role: str) -> int:
    """Calls in the current 1-minute window for `role`."""
    window = int(time.time() // 60)
    key = _RATE_KEY.format(role=role, window=window)
    val = r.get(key)
    return int(val) if val else 0
