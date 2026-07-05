"""Story 3.3 — per-story cost integrity surfacing (provenance flag + reconciliation)."""
from __future__ import annotations

import fakeredis
import pytest

from event_bus import telemetry as ebt
from reviewer import telemetry as tel


class _U:
    def __init__(self, c, i, o):
        self.cost, self.prompt_tokens, self.completion_tokens = c, i, o


class _R:
    def __init__(self, c, i, o):
        self.usage = _U(c, i, o)


class TestByStoryProvenance:
    def test_by_story_carries_coder_cost_source(self):
        r = fakeredis.FakeRedis()
        tel.record_usage(r, "coder", "m", _R(0.10, 100, 50), story="s1")
        r.set("coder_cost_src:s1", "json")
        row = next(x for x in ebt.get_telemetry_summary(r, days=1)["by_story"]
                   if x["story"] == "s1")
        assert row["coder_cost_source"] == "json"

    def test_unknown_when_no_flag(self):
        r = fakeredis.FakeRedis()
        tel.record_usage(r, "coder", "m", _R(0.10, 100, 50), story="s2")
        row = next(x for x in ebt.get_telemetry_summary(r, days=1)["by_story"]
                   if x["story"] == "s2")
        assert row["coder_cost_source"] == "unknown"


class TestReconciliation:
    def test_reports_unattributed_delta(self):
        r = fakeredis.FakeRedis()
        tel.record_usage(r, "coder", "m", _R(0.10, 100, 50), story="s1")  # attributed
        tel.record_usage(r, "planner", "m", _R(0.04, 40, 20))              # no story
        rec = ebt.get_telemetry_summary(r, days=1)["reconciliation"]
        assert rec["total_usd"] == pytest.approx(0.14)
        assert rec["story_attributed_usd"] == pytest.approx(0.10)
        assert rec["unattributed_usd"] == pytest.approx(0.04)
        assert rec["unattributed_pct"] == pytest.approx(0.04 / 0.14 * 100, abs=0.01)

    def test_zero_spend_is_safe(self):
        r = fakeredis.FakeRedis()
        rec = ebt.get_telemetry_summary(r, days=1)["reconciliation"]
        assert rec["unattributed_pct"] == 0.0
