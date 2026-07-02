"""
Phase 7 tests — sandboxing, rate/concurrency limits, cost telemetry, observability.
"""

from __future__ import annotations
from unittest.mock import MagicMock, patch, call
import json
import time

import pytest
import fakeredis

from event_bus.limits import (
    acquire_slot, release_slot, check_rate,
    get_concurrency, get_rejected, get_rate_window_count,
)
from event_bus.config_store import LimitsConfig, RuntimeConfig, patch_config, get_config


# ─────────────────────────────────────────────────────────────────────────────
# LimitsConfig persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestLimitsConfig:
    def test_defaults_persisted_and_read_back(self):
        r = fakeredis.FakeRedis()
        config = get_config(r)
        assert config.limits.max_concurrent_reviewer == 3
        assert config.limits.max_rpm_coder == 3

    def test_patch_limits_field(self):
        r = fakeredis.FakeRedis()
        result = patch_config(r, {"limits": {"max_concurrent_reviewer": 5}})
        assert result.limits.max_concurrent_reviewer == 5
        # other fields unchanged
        assert result.limits.max_rpm_coder == 3

    def test_patch_multiple_limits(self):
        r = fakeredis.FakeRedis()
        result = patch_config(r, {"limits": {
            "max_concurrent_coder": 4,
            "max_rpm_security": 20,
        }})
        assert result.limits.max_concurrent_coder == 4
        assert result.limits.max_rpm_security == 20

    def test_unknown_limit_field_ignored(self):
        r = fakeredis.FakeRedis()
        result = patch_config(r, {"limits": {"max_concurrent_dragons": 99}})
        assert not hasattr(result.limits, "max_concurrent_dragons")

    def test_cost_cap_coerced_to_float_counters_stay_int(self):
        r = fakeredis.FakeRedis()
        result = patch_config(r, {"limits": {"max_cost_usd_daily": 2.5, "max_rpm_coder": 7}})
        assert result.limits.max_cost_usd_daily == 2.5
        assert isinstance(result.limits.max_cost_usd_daily, float)
        assert result.limits.max_rpm_coder == 7
        assert isinstance(result.limits.max_rpm_coder, int)

    def test_limits_persist_across_reads(self):
        r = fakeredis.FakeRedis()
        patch_config(r, {"limits": {"max_rpm_reviewer": 42}})
        config = get_config(r)
        assert config.limits.max_rpm_reviewer == 42

    def test_limits_alongside_gates_and_models(self):
        r = fakeredis.FakeRedis()
        result = patch_config(r, {
            "gates": {"pr_merge_approval": True},
            "models": {"reviewer": "openai/gpt-4o"},
            "limits": {"max_concurrent_reviewer": 7},
        })
        assert result.gates.pr_merge_approval is True
        assert result.models.reviewer == "openai/gpt-4o"
        assert result.limits.max_concurrent_reviewer == 7


# ─────────────────────────────────────────────────────────────────────────────
# Concurrency limiting
# ─────────────────────────────────────────────────────────────────────────────

class TestConcurrencyLimits:
    def test_slot_acquired_and_released(self):
        r = fakeredis.FakeRedis()
        assert acquire_slot(r, "reviewer", 3) is True
        assert get_concurrency(r, "reviewer") == 1
        release_slot(r, "reviewer")
        assert get_concurrency(r, "reviewer") == 0

    def test_slot_denied_at_max(self):
        r = fakeredis.FakeRedis()
        acquire_slot(r, "reviewer", 2)
        acquire_slot(r, "reviewer", 2)
        assert acquire_slot(r, "reviewer", 2) is False
        assert get_concurrency(r, "reviewer") == 2

    def test_slot_available_after_release(self):
        r = fakeredis.FakeRedis()
        acquire_slot(r, "reviewer", 1)
        release_slot(r, "reviewer")
        assert acquire_slot(r, "reviewer", 1) is True

    def test_unlimited_zero_always_allows(self):
        r = fakeredis.FakeRedis()
        for _ in range(100):
            assert acquire_slot(r, "reviewer", 0) is True

    def test_rejected_counter_increments_on_denial(self):
        r = fakeredis.FakeRedis()
        acquire_slot(r, "tester", 1)
        acquire_slot(r, "tester", 1)  # denied
        assert get_rejected(r, "tester") == 1

    def test_release_underflow_guard(self):
        r = fakeredis.FakeRedis()
        # Should not go negative
        release_slot(r, "reviewer")
        assert get_concurrency(r, "reviewer") == 0


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiting
# ─────────────────────────────────────────────────────────────────────────────

class TestRateLimits:
    def test_within_limit_returns_true(self):
        r = fakeredis.FakeRedis()
        for _ in range(5):
            assert check_rate(r, "coder", 10) is True

    def test_exceeds_limit_returns_false(self):
        r = fakeredis.FakeRedis()
        for _ in range(10):
            check_rate(r, "coder", 10)
        assert check_rate(r, "coder", 10) is False

    def test_unlimited_zero_always_true(self):
        r = fakeredis.FakeRedis()
        for _ in range(200):
            assert check_rate(r, "coder", 0) is True

    def test_rate_window_count_reflects_calls(self):
        r = fakeredis.FakeRedis()
        check_rate(r, "reviewer", 20)
        check_rate(r, "reviewer", 20)
        assert get_rate_window_count(r, "reviewer") == 2

    def test_rejected_counter_increments_on_rate_hit(self):
        r = fakeredis.FakeRedis()
        for _ in range(3):
            check_rate(r, "security", 2)
        assert get_rejected(r, "security") >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox — process mode (default)
# ─────────────────────────────────────────────────────────────────────────────

class TestSandboxProcessMode:
    def test_runs_function_directly(self):
        from event_bus.sandbox import Sandbox
        sb = Sandbox(mode="process")
        result = sb.run(lambda x: {"doubled": x * 2}, x=7)
        assert result == {"doubled": 14}

    def test_passes_kwargs(self):
        from event_bus.sandbox import Sandbox
        sb = Sandbox(mode="process")

        def echo(a, b):
            return {"a": a, "b": b}

        assert sb.run(echo, a=1, b=2) == {"a": 1, "b": 2}

    def test_docker_mode_raises_without_docker_package(self):
        from event_bus.sandbox import Sandbox
        sb = Sandbox(mode="docker")
        with patch("builtins.__import__", side_effect=ImportError("no docker")):
            with pytest.raises(Exception):
                sb.run(lambda: {})

    def test_get_sandbox_returns_singleton(self):
        from event_bus import sandbox as sb_mod
        # Reset the module-level singleton for test isolation
        sb_mod._sandbox = None
        s1 = sb_mod.get_sandbox()
        s2 = sb_mod.get_sandbox()
        assert s1 is s2


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox — Docker mode (mocked)
# ─────────────────────────────────────────────────────────────────────────────

class TestSandboxDockerMode:
    def test_docker_run_passes_func_and_kwargs(self):
        from event_bus.sandbox import Sandbox

        mock_docker = MagicMock()
        mock_container_result = json.dumps({"status": "ok"}).encode()
        mock_docker.from_env.return_value.containers.run.return_value = mock_container_result

        with patch.dict("sys.modules", {"docker": mock_docker,
                                        "docker.errors": MagicMock()}):
            sb = Sandbox(mode="docker", image="test-image:latest")

            def dummy_func(repo_full_name, pr_number):
                return {}

            sb.run(dummy_func, repo_full_name="owner/repo", pr_number=1)

        run_call = mock_docker.from_env.return_value.containers.run
        assert run_call.called
        call_kwargs = run_call.call_args
        env = call_kwargs.kwargs.get("environment") or call_kwargs.args[1]
        assert "AGENT_FUNC" in env or (
            # env might be in positional args
            any("AGENT_FUNC" in str(a) for a in call_kwargs.args)
        )

    def test_parse_result_picks_last_json_among_logs(self):
        from event_bus.sandbox import _parse_result
        out = ('2026-01-01 log line before\n'
               '{"status": "pass", "role": "code_review"}\n'
               'trailing log noise\n')
        # real result is the JSON object even though a log line follows it
        assert _parse_result(out) == {"status": "pass", "role": "code_review"}

    def test_parse_result_none_when_no_json(self):
        from event_bus.sandbox import _parse_result
        assert _parse_result("just logs\nno json here\n") is None

    def test_docker_failure_surfaces_reason_from_output(self):
        # A crashed sandbox must log the real reason (from the container output), not the
        # empty string that stderr=False used to yield.
        from event_bus.sandbox import Sandbox
        errors_mod = MagicMock()
        class _ContainerError(Exception):
            def __init__(self, stderr, exit_status):
                self.stderr = stderr; self.exit_status = exit_status
        errors_mod.ContainerError = _ContainerError
        mock_docker = MagicMock()
        mock_docker.errors = errors_mod
        crash = _ContainerError(
            stderr=b'agent log\n{"status": "error", "reason": "litellm timeout"}\n',
            exit_status=1)
        mock_docker.from_env.return_value.containers.run.side_effect = crash

        with patch.dict("sys.modules", {"docker": mock_docker, "docker.errors": errors_mod}), \
             patch("event_bus.sandbox.log") as mlog:
            sb = Sandbox(mode="docker", image="test:latest")
            def f(x): return {}
            try:
                sb.run(f, x=1)
            except _ContainerError:
                pass
        # the real reason was extracted from the container output and logged
        kw = mlog.error.call_args.kwargs
        assert kw.get("reason") == "litellm timeout"
        assert kw.get("exit_code") == 1

    def test_scoped_env_reviewer_includes_forgejo(self):
        from event_bus.sandbox import _scoped_env
        import os
        with patch.dict(os.environ, {
            "FORGEJO_API_TOKEN": "tok123",
            "ANTHROPIC_API_KEY": "ant456",
            "DEFAULT_REPO": "should-not-appear",
        }):
            env = _scoped_env("reviewer.code_review")
        assert "FORGEJO_API_TOKEN" in env
        assert "ANTHROPIC_API_KEY" in env
        # DEFAULT_REPO belongs to the coding-agent group, not reviewer
        assert "DEFAULT_REPO" not in env

    def test_scoped_env_coding_agent_includes_forgejo(self):
        from event_bus.sandbox import _scoped_env
        import os
        with patch.dict(os.environ, {
            "FORGEJO_API_TOKEN": "fg-tok",
            "DEFAULT_REPO": "devadmin/sandbox",
            "ANTHROPIC_API_KEY": "ant456",
            "MODEL_REVIEWER": "should-not-appear",
        }):
            env = _scoped_env("coding_agent.main")
        assert "FORGEJO_API_TOKEN" in env
        assert "DEFAULT_REPO" in env
        assert "MODEL_REVIEWER" not in env


# ─────────────────────────────────────────────────────────────────────────────
# Telemetry — reviewer side
# ─────────────────────────────────────────────────────────────────────────────

class TestReviewerTelemetry:
    def test_record_usage_writes_redis_fields(self):
        from reviewer.telemetry import record_usage

        r = fakeredis.FakeRedis()
        mock_response = MagicMock()
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50

        with patch("litellm.completion_cost", return_value=0.001):
            record_usage(r, "code_review", "anthropic/claude-sonnet-4-6", mock_response)

        date = time.strftime("%Y-%m-%d", time.gmtime())
        raw = r.hgetall(f"telemetry:llm:{date}")
        keys = {k.decode() for k in raw}
        assert any("code_review" in k and "cost_usd" in k for k in keys)
        assert any("code_review" in k and "input_tokens" in k for k in keys)

    def test_record_usage_tolerates_missing_usage(self):
        from reviewer.telemetry import record_usage

        r = fakeredis.FakeRedis()
        mock_response = MagicMock()
        mock_response.usage = None  # no usage data
        # Should not raise
        record_usage(r, "code_review", "some-model", mock_response)

    def test_read_all_returns_records(self):
        from reviewer.telemetry import record_usage, read_all

        r = fakeredis.FakeRedis()
        mock_resp = MagicMock()
        mock_resp.usage.prompt_tokens = 200
        mock_resp.usage.completion_tokens = 100
        # Set cost=0 so record_usage falls through to litellm.completion_cost
        mock_resp.usage.cost = 0

        with patch("litellm.completion_cost", return_value=0.002):
            record_usage(r, "test_run", "openrouter/haiku", mock_resp)

        records = read_all(r, days=1)
        assert len(records) >= 1
        rec = records[0]
        assert rec["role"] == "test_run"
        assert rec["input_tokens"] == 200
        assert rec["output_tokens"] == 100
        assert abs(rec["cost_usd"] - 0.002) < 1e-6


# ─────────────────────────────────────────────────────────────────────────────
# Telemetry — event-bus aggregation
# ─────────────────────────────────────────────────────────────────────────────

class TestEventBusTelemetry:
    def test_summary_includes_all_roles(self):
        from event_bus.telemetry import get_telemetry_summary
        r = fakeredis.FakeRedis()
        summary = get_telemetry_summary(r, days=1)
        assert "by_role" in summary
        for role in ("coder", "reviewer", "tester", "security", "idea", "planner"):
            assert role in summary["by_role"]

    def test_summary_includes_concurrency_and_rejection_counts(self):
        from event_bus.telemetry import get_telemetry_summary
        r = fakeredis.FakeRedis()
        # Simulate one rejection
        acquire_slot(r, "reviewer", 1)
        acquire_slot(r, "reviewer", 1)  # denied → increment rejected

        summary = get_telemetry_summary(r, days=1)
        assert summary["by_role"]["reviewer"]["rate_rejected_total"] >= 1

    def test_prometheus_format_has_required_metrics(self):
        from event_bus.telemetry import render_prometheus
        r = fakeredis.FakeRedis()
        text = render_prometheus(r)
        assert "agent_concurrent_jobs" in text
        assert "agent_rate_rejected_total" in text
        assert "agent_rate_current_minute" in text

    def test_prometheus_format_has_role_labels(self):
        from event_bus.telemetry import render_prometheus
        r = fakeredis.FakeRedis()
        text = render_prometheus(r)
        assert 'role="reviewer"' in text
        assert 'role="tester"' in text

    def test_prometheus_includes_llm_metrics_when_data_present(self):
        from event_bus.telemetry import render_prometheus
        from reviewer.telemetry import record_usage

        r = fakeredis.FakeRedis()
        mock_resp = MagicMock()
        mock_resp.usage.prompt_tokens = 100
        mock_resp.usage.completion_tokens = 50

        with patch("litellm.completion_cost", return_value=0.005):
            record_usage(r, "reviewer", "claude-sonnet-4-6", mock_resp)

        text = render_prometheus(r)
        assert "agent_llm_cost_usd_today" in text
        assert "agent_llm_calls_today" in text
        assert "agent_llm_tokens_today" in text
        assert 'role="reviewer"' in text


# ─────────────────────────────────────────────────────────────────────────────
# API endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestTelemetryEndpoints:
    def test_get_telemetry_returns_summary(self, client):
        resp = client.get("/api/telemetry")
        assert resp.status_code == 200
        data = resp.json()
        assert "by_role" in data
        assert "total_cost_usd" in data

    def test_get_metrics_returns_prometheus_text(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers["content-type"]
        assert "agent_concurrent_jobs" in resp.text

    def test_health_includes_queue_depth(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "queue_depth" in data

    def test_get_config_includes_limits(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "limits" in data
        assert "max_concurrent_reviewer" in data["limits"]
        assert "max_rpm_coder" in data["limits"]

    def test_patch_config_updates_limits(self, client, monkeypatch):
        mock_r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", mock_r)
        resp = client.patch("/api/config", json={
            "limits": {"max_concurrent_coder": 4, "max_rpm_reviewer": 15}
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["limits"]["max_concurrent_coder"] == 4
        assert data["limits"]["max_rpm_reviewer"] == 15


# ─────────────────────────────────────────────────────────────────────────────
# pr_jobs concurrency integration
# ─────────────────────────────────────────────────────────────────────────────

class TestPrJobsRedisAndLimits:
    def test_reads_config_from_redis(self):
        """Covers the _redis_and_limits() try-block body."""
        r = fakeredis.FakeRedis()
        from event_bus.config_store import patch_config
        patch_config(r, {"limits": {"max_concurrent_reviewer": 7}})
        with patch("redis.from_url", return_value=r):
            from event_bus.jobs.pr_jobs import _redis_and_limits
            result_r, lim = _redis_and_limits()
        assert lim.max_concurrent_reviewer == 7

    def test_falls_back_to_defaults_on_error(self):
        """Covers the _redis_and_limits() except-block."""
        with patch("redis.from_url", side_effect=Exception("connection refused")):
            from event_bus.jobs.pr_jobs import _redis_and_limits
            result_r, lim = _redis_and_limits()
        assert result_r is None
        assert lim.max_concurrent_reviewer == 3  # LimitsConfig default

    def test_run_tester_success_path(self):
        """Covers run_tester sandbox.run call and finally release."""
        r = fakeredis.FakeRedis()
        # Use a fresh process-mode sandbox
        from event_bus.sandbox import Sandbox
        with (
            patch("event_bus.jobs.pr_jobs._redis_and_limits",
                  return_value=(r, LimitsConfig(max_concurrent_tester=3))),
            patch("event_bus.sandbox.get_sandbox", return_value=Sandbox(mode="process")),
            patch("reviewer.test_runner.run_tests", return_value={"status": "pass"}),
        ):
            from event_bus.jobs.pr_jobs import run_tester
            result = run_tester("owner/repo", 1, "abc123")
        assert result["status"] == "pass"
        assert get_concurrency(r, "tester") == 0

    def test_run_security_scanner_success_path(self):
        """Covers run_security_scanner sandbox.run call and finally release."""
        r = fakeredis.FakeRedis()
        from event_bus.sandbox import Sandbox
        with (
            patch("event_bus.jobs.pr_jobs._redis_and_limits",
                  return_value=(r, LimitsConfig(max_concurrent_security=3))),
            patch("event_bus.sandbox.get_sandbox", return_value=Sandbox(mode="process")),
            patch("reviewer.security_scan.run_security_scan", return_value={"status": "pass"}),
        ):
            from event_bus.jobs.pr_jobs import run_security_scanner
            result = run_security_scanner("owner/repo", 1, "abc123")
        assert result["status"] == "pass"
        assert get_concurrency(r, "security") == 0

    def test_run_tester_rejects_at_max_concurrency(self):
        r = fakeredis.FakeRedis()
        acquire_slot(r, "tester", 1)
        with patch("event_bus.jobs.pr_jobs._redis_and_limits",
                   return_value=(r, LimitsConfig(max_concurrent_tester=1))):
            from event_bus.jobs.pr_jobs import run_tester
            result = run_tester("owner/repo", 1, "abc123")
        assert result["status"] == "error"
        assert "concurrency" in result["reason"]


class TestPrJobsConcurrency:
    def test_run_code_reviewer_acquires_and_releases_slot(self):
        r = fakeredis.FakeRedis()
        with (
            patch("event_bus.jobs.pr_jobs._redis_and_limits",
                  return_value=(r, LimitsConfig(max_concurrent_reviewer=3))),
            patch("event_bus.sandbox.get_sandbox") as mock_sandbox,
            patch("reviewer.code_review.run_code_review", return_value={"status": "pass"}),
        ):
            mock_sandbox.return_value.run.side_effect = lambda f, **kw: f(**kw)
            from event_bus.jobs.pr_jobs import run_code_reviewer
            run_code_reviewer("owner/repo", 1, "abc123")

        # Slot should be released after function returns
        assert get_concurrency(r, "reviewer") == 0

    def test_run_code_reviewer_rejects_at_max_concurrency(self):
        r = fakeredis.FakeRedis()
        # Fill the single slot
        acquire_slot(r, "reviewer", 1)

        with patch("event_bus.jobs.pr_jobs._redis_and_limits",
                   return_value=(r, LimitsConfig(max_concurrent_reviewer=1))):
            from event_bus.jobs.pr_jobs import run_code_reviewer
            result = run_code_reviewer("owner/repo", 1, "abc123")

        assert result["status"] == "error"
        assert "concurrency" in result["reason"]


# ─────────────────────────────────────────────────────────────────────────────
# handlers rate limit integration
# ─────────────────────────────────────────────────────────────────────────────

class TestHandlersRateLimit:
    def test_handle_pr_event_skips_rate_limited_roles(self):
        r = fakeredis.FakeRedis()
        # Pre-populate config: reviewer limited to 1 RPM, others unlimited
        from event_bus.config_store import patch_config
        patch_config(r, {"limits": {
            "max_rpm_reviewer": 1,
            "max_rpm_tester": 0,
            "max_rpm_security": 0,
        }})
        # Exhaust reviewer's 1-call-per-minute allowance
        check_rate(r, "reviewer", 1)

        mock_q = MagicMock()
        job = MagicMock()
        job.id = "job-1"
        mock_q.enqueue.return_value = job

        with (
            patch("redis.from_url", return_value=r),
            patch("rq.Queue", return_value=mock_q),
        ):
            from event_bus.jobs.handlers import handle_pr_event
            result = handle_pr_event("owner/repo", 1, "abc123", "opened")

        assert result["status"] == "dispatched"
        assert "reviewer" in result["rate_limited"]
        # Only 2 jobs enqueued (tester + security), not reviewer
        assert mock_q.enqueue.call_count == 2


# ── EPIC 6.2: per-stack telemetry in the summary ──────────────────────────────

class TestTelemetryByStack:
    def test_summary_includes_by_stack(self):
        from event_bus.telemetry import get_telemetry_summary
        from reviewer.telemetry import record_usage
        r = fakeredis.FakeRedis()
        resp = MagicMock()
        resp.usage.prompt_tokens = 50
        resp.usage.completion_tokens = 20
        resp.usage.cost = 0.0
        with patch("litellm.completion_cost", return_value=0.001):
            record_usage(r, "reviewer", "m", resp, stack="python")
            record_usage(r, "planner", "m", resp, stack="python")
            record_usage(r, "reviewer", "m", resp, stack="go")
        summary = get_telemetry_summary(r, days=1)
        assert "by_stack" in summary
        assert summary["by_stack"]["python"]["calls"] == 2
        assert summary["by_stack"]["go"]["calls"] == 1
