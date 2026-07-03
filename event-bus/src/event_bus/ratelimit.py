"""
Upstream rate-limit circuit breaker (per model).

Distinct from free_guard (which enforces the *daily* free-request cap): this handles the
*burst* 429s a provider returns when a model — typically a `:free` one — is temporarily
rate-limited upstream. When a model 429s, we trip a per-model breaker with an exponential
backoff (honoring the provider's retry_after). While tripped, the coder dispatch and the
verdict fan-out HOLD affected stories in place instead of running into a guaranteed
failure, and the watchdog treats them as "paused", not "stuck".

Recovery is automatic and needs no active prober: the breaker key carries a TTL, so when
it expires the next attempt runs — a success clears the breaker, another 429 re-trips it
with a longer backoff. So the fleet self-heals whenever the limits lift, even hours later.

Keys (best-effort; a telemetry failure must never break the pipeline):
  ratelimited:{model}  → expiry epoch, TTL = current backoff
  rl_strikes:{model}   → consecutive-trip count for exponential backoff
"""
from __future__ import annotations
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import redis

_BASE_BACKOFF = 45      # first pause, seconds
_MAX_BACKOFF = 900      # cap at 15 min
_STRIKE_TTL = 3600      # forget the strike streak after an hour of calm


def _norm(model: str) -> str:
    return (model or "").strip()


def trip(r: "redis.Redis", model: str, retry_after: Optional[float] = None) -> int:
    """Record a 429 for `model` and (re)arm its breaker with an exponential backoff.
    Returns the chosen backoff in seconds (0 if it couldn't be recorded)."""
    model = _norm(model)
    if r is None or not model:
        return 0
    try:
        strikes = int(r.incr(f"rl_strikes:{model}"))
        r.expire(f"rl_strikes:{model}", _STRIKE_TTL)
        backoff = min(_MAX_BACKOFF, _BASE_BACKOFF * (2 ** min(strikes - 1, 4)))
        if retry_after:
            backoff = min(_MAX_BACKOFF, max(backoff, int(retry_after) + 2))
        r.setex(f"ratelimited:{model}", int(backoff), str(int(time.time()) + int(backoff)))
        return int(backoff)
    except Exception:
        return 0


def is_tripped(r: "redis.Redis", model: str) -> bool:
    """True if `model` is currently rate-limit-paused."""
    model = _norm(model)
    if r is None or not model:
        return False
    try:
        return bool(r.get(f"ratelimited:{model}"))
    except Exception:
        return False


def clear(r: "redis.Redis", model: str) -> None:
    """A call to `model` succeeded — reset its breaker and strike streak."""
    model = _norm(model)
    if r is None or not model:
        return
    try:
        r.delete(f"ratelimited:{model}", f"rl_strikes:{model}")
    except Exception:
        pass


def tripped_models(r: "redis.Redis") -> list[dict]:
    """[{model, seconds_left}] for every currently paused model — for the UI banner."""
    out: list[dict] = []
    if r is None:
        return out
    try:
        for key in r.scan_iter(match="ratelimited:*"):
            k = key.decode() if isinstance(key, bytes) else key
            model = k.split("ratelimited:", 1)[1]
            ttl = r.ttl(key)
            out.append({"model": model, "seconds_left": int(ttl) if ttl and ttl > 0 else 0})
    except Exception:
        return out
    return sorted(out, key=lambda x: -x["seconds_left"])
