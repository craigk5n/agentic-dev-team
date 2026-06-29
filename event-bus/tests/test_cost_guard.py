"""Tests for event_bus.cost_guard — daily LLM spend backstop."""

from unittest.mock import MagicMock, patch

import fakeredis

from event_bus.cost_guard import today_spend, over_budget


def _record(r, cost_usd):
    """Write one telemetry record with the given cost (via reviewer.telemetry)."""
    from reviewer.telemetry import record_usage
    resp = MagicMock()
    resp.usage.prompt_tokens = 10
    resp.usage.completion_tokens = 5
    resp.usage.cost = None  # force the litellm.completion_cost path
    with patch("litellm.completion_cost", return_value=cost_usd):
        record_usage(r, "reviewer", "openrouter/m", resp)


class TestTodaySpend:
    def test_zero_when_no_records(self):
        assert today_spend(fakeredis.FakeRedis()) == 0.0

    def test_sums_today_records(self):
        r = fakeredis.FakeRedis()
        _record(r, 0.02)
        _record(r, 0.03)
        assert abs(today_spend(r) - 0.05) < 1e-6


class TestOverBudget:
    def test_cap_zero_is_unlimited(self):
        r = fakeredis.FakeRedis()
        _record(r, 5.0)
        assert over_budget(r, 0.0) is False

    def test_under_cap_allows(self):
        r = fakeredis.FakeRedis()
        _record(r, 0.01)
        assert over_budget(r, 1.0) is False

    def test_at_cap_blocks(self):
        r = fakeredis.FakeRedis()
        _record(r, 0.50)
        assert over_budget(r, 0.50) is True

    def test_over_cap_blocks(self):
        r = fakeredis.FakeRedis()
        _record(r, 0.75)
        assert over_budget(r, 0.50) is True
