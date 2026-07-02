"""Tests for event_bus.free_guard — OpenRouter free-tier request backstop."""

from unittest.mock import MagicMock

import fakeredis

from event_bus.free_guard import (
    free_calls_today, free_quota_status, free_quota_exceeded,
)


def _record(r, model, n=1):
    """Write n telemetry calls for a model (via reviewer.telemetry)."""
    from reviewer.telemetry import record_usage
    for _ in range(n):
        resp = MagicMock()
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5
        resp.usage.cost = 0.0  # free models cost $0
        record_usage(r, "reviewer", model, resp)


class TestFreeCallsToday:
    def test_zero_when_none(self):
        assert free_calls_today(fakeredis.FakeRedis()) == 0

    def test_counts_only_free_models(self):
        r = fakeredis.FakeRedis()
        _record(r, "openrouter/nvidia/nemotron-3-super-120b-a12b:free", n=3)
        _record(r, "openrouter/meta-llama/llama-3.3-70b-instruct:free", n=2)
        _record(r, "openrouter/minimax/minimax-m2.5", n=5)          # paid — excluded
        _record(r, "claude-code/opus", n=4)                          # subscription — excluded
        assert free_calls_today(r) == 5


class TestFreeQuotaStatus:
    def test_disabled_when_limit_zero(self):
        r = fakeredis.FakeRedis()
        _record(r, "x:free", n=100)
        s = free_quota_status(r, 0)
        assert s["limit"] == 0 and s["warn"] is False and s["exceeded"] is False
        assert s["used"] == 100          # usage still reported

    def test_warns_at_80_percent(self):
        r = fakeredis.FakeRedis()
        _record(r, "m:free", n=80)
        s = free_quota_status(r, 100)
        assert s["warn"] is True and s["exceeded"] is False
        assert s["pct"] == 80.0 and s["remaining"] == 20

    def test_below_threshold_no_warn(self):
        r = fakeredis.FakeRedis()
        _record(r, "m:free", n=50)
        assert free_quota_status(r, 100)["warn"] is False

    def test_exceeded_at_limit(self):
        r = fakeredis.FakeRedis()
        _record(r, "m:free", n=100)
        s = free_quota_status(r, 100)
        assert s["exceeded"] is True and s["remaining"] == 0


class TestFreeQuotaExceeded:
    def test_limit_zero_never_exceeds(self):
        r = fakeredis.FakeRedis()
        _record(r, "m:free", n=9999)
        assert free_quota_exceeded(r, 0) is False

    def test_blocks_at_limit(self):
        r = fakeredis.FakeRedis()
        _record(r, "m:free", n=1000)
        assert free_quota_exceeded(r, 1000) is True

    def test_allows_under_limit(self):
        r = fakeredis.FakeRedis()
        _record(r, "m:free", n=999)
        assert free_quota_exceeded(r, 1000) is False
