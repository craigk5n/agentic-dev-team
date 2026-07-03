"""Tests for per-model outcome capture (outcomes.py) and the /api/metrics endpoint."""
from __future__ import annotations

import fakeredis
import pytest

from event_bus import outcomes as O


class TestOutcomes:
    def test_record_and_read_roundtrip(self):
        r = fakeredis.FakeRedis()
        for _ in range(3):
            O.record_outcome(r, "coder", "openrouter/x:free", "merged")
        O.record_outcome(r, "coder", "openrouter/x:free", "abandoned")
        got = O.read_outcomes(r, days=1)
        # model id containing ':' must survive the split
        assert got[("coder", "openrouter/x:free")] == {"merged": 3, "abandoned": 1}

    def test_empty_model_or_outcome_is_ignored(self):
        r = fakeredis.FakeRedis()
        O.record_outcome(r, "coder", "", "merged")
        O.record_outcome(r, "coder", "m", "")
        assert O.read_outcomes(r, days=1) == {}

    def test_none_redis_is_safe(self):
        O.record_outcome(None, "coder", "m", "merged")   # no raise
        assert O.read_outcomes(None) == {}

    def test_latency_averages_per_model(self):
        r = fakeredis.FakeRedis()
        O.record_latency(r, "coder", "openrouter/x:free", 1000)
        O.record_latency(r, "coder", "openrouter/x:free", 3000)
        O.record_latency(r, "reviewer", "n:free", 500)
        lat = O.read_latency(r, days=1)
        assert lat[("coder", "openrouter/x:free")] == 2000   # (1000+3000)/2
        assert lat[("reviewer", "n:free")] == 500

    def test_latency_ignores_bad_input(self):
        r = fakeredis.FakeRedis()
        O.record_latency(r, "coder", "", 100)
        O.record_latency(r, "coder", "m", -5)
        assert O.read_latency(r, days=1) == {}


class TestMetricsEndpoint:
    def test_metrics_computes_coder_rates(self, client, monkeypatch):
        r = fakeredis.FakeRedis(decode_responses=False)
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        # Seed outcomes: 8 merged (5 first-pass), 2 abandoned, 1 escalated
        for _ in range(8):
            O.record_outcome(r, "coder", "openrouter/minimax/minimax-m2.5", "merged")
        for _ in range(5):
            O.record_outcome(r, "coder", "openrouter/minimax/minimax-m2.5", "first_pass")
        for _ in range(2):
            O.record_outcome(r, "coder", "openrouter/minimax/minimax-m2.5", "abandoned")
        O.record_outcome(r, "coder", "openrouter/minimax/minimax-m2.5", "escalated")
        # Seed some volume so cost/success is exercised
        import time
        key = f"telemetry:llm:{time.strftime('%Y-%m-%d', time.gmtime())}"
        pfx = "coder:openrouter/minimax/minimax-m2.5"
        r.hincrbyfloat(key, f"{pfx}:cost_usd", 2.0)
        r.hincrby(key, f"{pfx}:calls", 20)

        resp = client.get("/api/metrics")
        assert resp.status_code == 200
        data = resp.json()
        coder = next(m for m in data["models"] if m["role"] == "coder")
        assert coder["success_rate"] == 0.8          # 8 / (8+2)
        assert coder["first_pass_rate"] == 0.625      # 5 / 8
        assert coder["escalation_rate"] == 0.1        # 1 / 10
        assert coder["cost_per_success"] == 0.25      # 2.0 / 8
        assert data["coder_compare"] and data["coder_compare"][0]["role"] == "coder"

    def test_metrics_reviewer_reliability(self, client, monkeypatch):
        r = fakeredis.FakeRedis(decode_responses=False)
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        for _ in range(7):
            O.record_outcome(r, "reviewer", "nemotron:free", "pass")
        for _ in range(2):
            O.record_outcome(r, "reviewer", "nemotron:free", "fail")
        O.record_outcome(r, "reviewer", "nemotron:free", "error")
        data = client.get("/api/metrics").json()
        rev = next(m for m in data["models"] if m["role"] == "reviewer")
        assert rev["reliability"] == 0.9              # (7+2) / 10
        assert rev["pass_rate"] == round(7/9, 3)      # pass / (pass+fail)

    def test_metrics_flip_ratelimit_latency(self, client, monkeypatch):
        r = fakeredis.FakeRedis(decode_responses=False)
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        for _ in range(8):
            O.record_outcome(r, "reviewer", "n:free", "pass")
        for _ in range(2):
            O.record_outcome(r, "reviewer", "n:free", "fail")
        O.record_outcome(r, "reviewer", "n:free", "flip")     # 1 flip of 10 verdicts
        O.record_outcome(r, "reviewer", "n:free", "rate_limited")  # 1 of 11 attempts
        O.record_latency(r, "reviewer", "n:free", 4000)
        O.record_latency(r, "reviewer", "n:free", 2000)
        rev = next(m for m in client.get("/api/metrics").json()["models"] if m["role"] == "reviewer")
        assert rev["flip_rate"] == 0.1                # 1 / (8+2)
        assert rev["rate_limit_rate"] == round(1/11, 3)   # rl / (pass+fail+error+rl)
        assert rev["avg_latency_ms"] == 3000

    def test_metrics_has_trend_and_by_stack(self, client, monkeypatch):
        r = fakeredis.FakeRedis(decode_responses=False)
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        data = client.get("/api/metrics?days=7").json()
        # trend: one entry per day in the window, oldest→newest, with the expected keys
        assert len(data["trend"]) == 7
        assert data["trend"][0]["date"] < data["trend"][-1]["date"]
        assert set(data["trend"][0]) == {"date", "calls", "cost_usd", "completed"}
        # by_stack present (list; each has success_rate + counts)
        assert isinstance(data["by_stack"], list)
        for s in data["by_stack"]:
            assert {"stack", "done", "abandoned", "in_flight", "success_rate", "avg_cycle_secs"} <= set(s)

    def test_model_spark_length_matches_window(self, client, monkeypatch):
        import time
        r = fakeredis.FakeRedis(decode_responses=False)
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        key = f"telemetry:llm:{time.strftime('%Y-%m-%d', time.gmtime())}"
        r.hincrby(key, "coder:openrouter/qwen/qwen3-coder:free:calls", 4)
        data = client.get("/api/metrics?days=5").json()
        coder = next(m for m in data["models"] if m["role"] == "coder")
        assert len(coder["spark"]) == 5           # one point per day
        assert coder["spark"][-1] == 4            # today's calls land in the last slot
