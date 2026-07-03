"""Tests for the rate-limit circuit breaker + its enforcement in the verdict fan-out."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from event_bus import ratelimit as R


class TestBreaker:
    def test_trip_sets_and_is_tripped_reads(self):
        r = fakeredis.FakeRedis()
        assert R.is_tripped(r, "m:free") is False
        R.trip(r, "m:free")
        assert R.is_tripped(r, "m:free") is True

    def test_backoff_is_exponential_and_capped(self):
        r = fakeredis.FakeRedis()
        backoffs = [R.trip(r, "m") for _ in range(7)]
        assert backoffs[0] == R._BASE_BACKOFF
        assert backoffs[1] > backoffs[0]                 # grows
        assert all(b <= R._MAX_BACKOFF for b in backoffs)  # never exceeds the cap
        assert backoffs[-1] == backoffs[-2]              # plateaus at the top step

    def test_retry_after_is_honored(self):
        r = fakeredis.FakeRedis()
        assert R.trip(r, "m", retry_after=600) >= 600

    def test_clear_resets_breaker_and_strikes(self):
        r = fakeredis.FakeRedis()
        R.trip(r, "m"); R.trip(r, "m")
        R.clear(r, "m")
        assert R.is_tripped(r, "m") is False
        assert R.trip(r, "m") == R._BASE_BACKOFF          # streak reset → back to base

    def test_tripped_models_lists_paused(self):
        r = fakeredis.FakeRedis()
        R.trip(r, "a:free"); R.trip(r, "b:free")
        models = {m["model"] for m in R.tripped_models(r)}
        assert models == {"a:free", "b:free"}

    def test_none_redis_and_empty_model_safe(self):
        assert R.trip(None, "m") == 0
        assert R.trip(fakeredis.FakeRedis(), "") == 0
        assert R.is_tripped(None, "m") is False
        assert R.tripped_models(None) == []


class TestFanOutHold:
    def test_fanout_holds_when_verdict_model_tripped(self, monkeypatch):
        # When a verdict model is rate-limited, handle_pr_event must NOT enqueue a partial
        # verdict set — it holds the PR so the watchdog re-queues once the breaker clears.
        from event_bus.jobs import handlers
        r = fakeredis.FakeRedis(decode_responses=False)
        R.trip(r, "openrouter/nvidia/nemotron-3-super-120b-a12b:free")

        from event_bus.config_store import RuntimeConfig, ModelConfig, LimitsConfig, GateConfig
        cfg = RuntimeConfig(
            gates=GateConfig(),
            models=ModelConfig(reviewer="openrouter/nvidia/nemotron-3-super-120b-a12b:free",
                               tester="x", security="y"),
            limits=LimitsConfig(max_concurrent_reviewer=5, max_concurrent_tester=5,
                                max_concurrent_security=5),
        )
        q = MagicMock()
        with patch("redis.from_url", return_value=r), \
             patch("event_bus.config_store.get_config", return_value=cfg), \
             patch("event_bus.prompt_store.get_prompt", return_value="p"), \
             patch("event_bus.cost_guard.over_budget", return_value=False), \
             patch("event_bus.limits.check_rate", return_value=True), \
             patch("rq.Queue", return_value=q):
            out = handlers.handle_pr_event("o/r", 5, "sha", "synchronized")
        assert out["status"] == "rate_limit_hold"
        q.enqueue.assert_not_called()
