"""Tests for reviewer.telemetry — LLM cost/token recording and retrieval."""

from __future__ import annotations
import time
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from reviewer.telemetry import record_usage, read_all


class TestRecordUsage:
    def test_writes_cost_and_tokens_to_redis(self):
        r = fakeredis.FakeRedis()
        resp = MagicMock()
        resp.usage.prompt_tokens = 200
        resp.usage.completion_tokens = 80

        with patch("litellm.completion_cost", return_value=0.003):
            record_usage(r, "code_review", "anthropic/claude-sonnet", resp)

        date = time.strftime("%Y-%m-%d")
        raw = r.hgetall(f"telemetry:llm:{date}")
        keys = {k.decode() for k in raw.keys()}

        assert any("cost_usd" in k for k in keys)
        assert any("input_tokens" in k for k in keys)
        assert any("output_tokens" in k for k in keys)
        assert any("calls" in k for k in keys)

    def test_accumulates_multiple_calls(self):
        r = fakeredis.FakeRedis()
        resp = MagicMock()
        resp.usage.prompt_tokens = 100
        resp.usage.completion_tokens = 50

        with patch("litellm.completion_cost", return_value=0.001):
            record_usage(r, "test_run", "haiku", resp)
            record_usage(r, "test_run", "haiku", resp)

        date = time.strftime("%Y-%m-%d")
        raw = r.hgetall(f"telemetry:llm:{date}")
        decoded = {k.decode(): v.decode() for k, v in raw.items()}
        assert int(decoded["test_run:haiku:calls"]) == 2
        assert int(decoded["test_run:haiku:input_tokens"]) == 200

    def test_tolerates_missing_usage_attribute(self):
        r = fakeredis.FakeRedis()
        resp = MagicMock()
        resp.usage = None
        # Should not raise
        record_usage(r, "code_review", "some-model", resp)
        date = time.strftime("%Y-%m-%d")
        assert r.hgetall(f"telemetry:llm:{date}") == {}

    def test_tolerates_litellm_cost_failure(self):
        r = fakeredis.FakeRedis()
        resp = MagicMock()
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5

        with patch("litellm.completion_cost", side_effect=Exception("unknown model")):
            # Should not raise — cost defaults to 0.0
            record_usage(r, "security", "unknown-model", resp)

        date = time.strftime("%Y-%m-%d")
        raw = r.hgetall(f"telemetry:llm:{date}")
        assert len(raw) > 0  # tokens still recorded

    def test_sets_ttl_on_key(self):
        r = fakeredis.FakeRedis()
        resp = MagicMock()
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5

        with patch("litellm.completion_cost", return_value=0.0):
            record_usage(r, "code_review", "model", resp)

        date = time.strftime("%Y-%m-%d")
        ttl = r.ttl(f"telemetry:llm:{date}")
        assert ttl > 0

    def test_handles_redis_write_failure_silently(self):
        r = MagicMock()
        r.hgetall.return_value = {}
        r.pipeline.side_effect = Exception("redis down")
        resp = MagicMock()
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5

        with patch("litellm.completion_cost", return_value=0.001):
            # Should not raise
            record_usage(r, "code_review", "model", resp)


class TestReadAll:
    def test_returns_empty_list_when_no_data(self):
        r = fakeredis.FakeRedis()
        records = read_all(r, days=1)
        assert records == []

    def test_returns_today_record_with_correct_fields(self):
        r = fakeredis.FakeRedis()
        resp = MagicMock()
        resp.usage.prompt_tokens = 300
        resp.usage.completion_tokens = 100
        resp.usage.cost = None  # ensure litellm.completion_cost path is taken

        with patch("litellm.completion_cost", return_value=0.005):
            record_usage(r, "reviewer", "openrouter/claude", resp)

        records = read_all(r, days=1)
        assert len(records) == 1
        rec = records[0]
        assert rec["role"] == "reviewer"
        assert rec["model"] == "openrouter/claude"
        assert rec["input_tokens"] == 300
        assert rec["output_tokens"] == 100
        assert abs(rec["cost_usd"] - 0.005) < 1e-6
        assert rec["calls"] == 1

    def test_aggregates_same_role_model(self):
        r = fakeredis.FakeRedis()
        resp = MagicMock()
        resp.usage.prompt_tokens = 100
        resp.usage.completion_tokens = 50

        with patch("litellm.completion_cost", return_value=0.001):
            record_usage(r, "tester", "haiku", resp)
            record_usage(r, "tester", "haiku", resp)

        records = read_all(r, days=1)
        assert len(records) == 1
        assert records[0]["calls"] == 2
        assert records[0]["input_tokens"] == 200

    def test_separates_different_roles(self):
        r = fakeredis.FakeRedis()
        resp = MagicMock()
        resp.usage.prompt_tokens = 100
        resp.usage.completion_tokens = 50

        with patch("litellm.completion_cost", return_value=0.001):
            record_usage(r, "code_review", "sonnet", resp)
            record_usage(r, "security", "haiku", resp)

        records = read_all(r, days=1)
        roles = {r["role"] for r in records}
        assert "code_review" in roles
        assert "security" in roles

    def test_sorted_by_date_role_model(self):
        r = fakeredis.FakeRedis()
        resp = MagicMock()
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5

        with patch("litellm.completion_cost", return_value=0.001):
            record_usage(r, "z_role", "model", resp)
            record_usage(r, "a_role", "model", resp)

        records = read_all(r, days=1)
        roles = [rec["role"] for rec in records]
        assert roles == sorted(roles)

    def test_tolerates_redis_failure_per_day(self):
        r = fakeredis.FakeRedis()
        # hgetall raises for some days — should return partial results
        original_hgetall = r.hgetall

        def patched_hgetall(key):
            if "2020" in key:
                raise Exception("io error")
            return original_hgetall(key)

        r.hgetall = patched_hgetall
        # Should not raise — just returns empty
        records = read_all(r, days=3)
        assert isinstance(records, list)
