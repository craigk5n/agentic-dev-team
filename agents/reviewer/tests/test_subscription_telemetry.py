"""Story 3.2 — attribute Claude Code subscription spend instead of discarding it."""
from __future__ import annotations

import json
import sys
import time
import types
from unittest.mock import patch

import fakeredis
import pytest

from reviewer.telemetry import record_subscription_usage, read_story_usage
from planner_agent import claude_code


def _today_llm_key():
    return f"telemetry:llm:{time.strftime('%Y-%m-%d', time.gmtime())}"


class TestRecordSubscriptionUsage:
    def test_records_tokens_and_known_cost(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        record_subscription_usage(r, "reviewer", "claude-code/sonnet",
                                  {"input_tokens": 1200, "output_tokens": 300,
                                   "cost_usd": 0.09, "cost_known": True},
                                  story="story-1")
        h = r.hgetall(_today_llm_key())
        pfx = "coder" if False else "reviewer:claude-code/sonnet"
        assert int(h[f"{pfx}:input_tokens"]) == 1200
        assert int(h[f"{pfx}:output_tokens"]) == 300
        assert abs(float(h[f"{pfx}:cost_usd"]) - 0.09) < 1e-9
        assert int(h[f"{pfx}:calls"]) == 1
        # per-story rollup present (no more $0/0-tokens for claude-code stories)
        srow = next(x for x in read_story_usage(r, days=1) if x["story"] == "story-1")
        assert srow["input_tokens"] == 1200

    def test_unpriced_records_tokens_with_zero_cost(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        record_subscription_usage(r, "reviewer", "claude-code/opus",
                                  {"input_tokens": 500, "output_tokens": 100,
                                   "cost_known": False})
        h = r.hgetall(_today_llm_key())
        pfx = "reviewer:claude-code/opus"
        assert int(h[f"{pfx}:input_tokens"]) == 500   # tokens captured
        assert float(h[f"{pfx}:cost_usd"]) == 0.0     # never fabricates a price


class TestClaudeCodeUsageOut:
    def test_complete_populates_usage_out(self):
        envelope = {"result": "done", "usage": {"input_tokens": 42, "output_tokens": 7},
                    "total_cost_usd": 0.013}
        proc = types.SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr="")
        usage: dict = {}
        with patch("planner_agent.claude_code.subprocess.run", return_value=proc):
            out = claude_code.complete([{"role": "user", "content": "hi"}],
                                       model="claude-code/sonnet", usage_out=usage)
        assert out == "done"
        assert usage["input_tokens"] == 42 and usage["output_tokens"] == 7
        assert usage["cost_usd"] == 0.013 and usage["cost_known"] is True
        assert usage["source"] == "subscription"

    def test_complete_marks_cost_unknown_when_absent(self):
        envelope = {"result": "ok", "usage": {"input_tokens": 5, "output_tokens": 1}}
        proc = types.SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr="")
        usage: dict = {}
        with patch("planner_agent.claude_code.subprocess.run", return_value=proc):
            claude_code.complete([{"role": "user", "content": "hi"}],
                                 model="claude-code/opus", usage_out=usage)
        assert usage["cost_known"] is False and usage["cost_usd"] == 0.0


class TestLlmClaudeCodeRecordsTelemetry:
    def test_subscription_usage_recorded_via_complete(self):
        from reviewer.llm import complete
        fake = types.ModuleType("planner_agent.claude_code")
        fake.is_claude_code_model = lambda m: str(m).startswith("claude-code")

        def _fake_complete(messages, model="", timeout=90.0, usage_out=None):
            if usage_out is not None:
                usage_out.update({"input_tokens": 10, "output_tokens": 4,
                                  "cost_usd": 0.02, "cost_known": True,
                                  "source": "subscription"})
            return "VERDICT"
        fake.complete = _fake_complete
        pkg = types.ModuleType("planner_agent"); pkg.claude_code = fake
        r = fakeredis.FakeRedis(decode_responses=True)
        rec = {}

        def _capture(_r, role, model, usage, **kw):
            rec.update({"role": role, "model": model, "usage": usage, "kw": kw})

        with patch.dict(sys.modules, {"planner_agent": pkg, "planner_agent.claude_code": fake}), \
             patch("reviewer.llm.litellm.completion") as mock_litellm, \
             patch("reviewer.telemetry.record_subscription_usage", _capture), \
             patch("redis.from_url", return_value=r):
            out = complete("claude-code/sonnet", [{"role": "user", "content": "q"}],
                           telemetry_role="reviewer", telemetry_story="story-9")
        assert out == "VERDICT"
        mock_litellm.assert_not_called()
        assert rec["role"] == "reviewer" and rec["model"] == "claude-code/sonnet"
        assert rec["usage"]["input_tokens"] == 10
        assert rec["kw"].get("story") == "story-9"
