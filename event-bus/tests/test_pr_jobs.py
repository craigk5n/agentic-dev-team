"""Tests for Phase 4 PR review fan-out — handlers and pr_job wrappers."""

from unittest.mock import MagicMock, patch
import fakeredis
import pytest

from event_bus.config_store import LimitsConfig


# ── helpers ────────────────────────────────────────────────────────────────────

def _no_limits():
    """Return (fakeredis, unlimited LimitsConfig) for patching _redis_and_limits."""
    return fakeredis.FakeRedis(), LimitsConfig(
        max_concurrent_reviewer=0, max_concurrent_tester=0, max_concurrent_security=0,
    )


def _direct_sandbox():
    """Sandbox mock that calls the function in-process with its kwargs."""
    mock_sb = MagicMock()
    mock_sb.run.side_effect = lambda func, **kw: func(**kw)
    return mock_sb


# ── pr_jobs wrappers ─────────────────────────────────────────────────────────

class TestPrJobWrappers:
    _kwargs = dict(repo_full_name="alice/backend", pr_number=7, head_sha="a" * 40, base_ref="main")

    def test_run_code_reviewer_delegates(self):
        from event_bus.jobs.pr_jobs import run_code_reviewer
        with (
            patch("event_bus.jobs.pr_jobs._redis_and_limits", return_value=_no_limits()),
            patch("event_bus.sandbox.get_sandbox", return_value=_direct_sandbox()),
            patch("reviewer.code_review.run_code_review", return_value={"status": "pass"}) as mock,
        ):
            result = run_code_reviewer(**self._kwargs, model_override="")
        mock.assert_called_once_with(
            repo_full_name="alice/backend", pr_number=7,
            head_sha="a" * 40, base_ref="main", head_ref="", model_override="",
            system_prompt="", task_prompt="", stack="",
        )
        assert result["status"] == "pass"

    def test_run_tester_delegates(self):
        from event_bus.jobs.pr_jobs import run_tester
        with (
            patch("event_bus.jobs.pr_jobs._redis_and_limits", return_value=_no_limits()),
            patch("event_bus.sandbox.get_sandbox", return_value=_direct_sandbox()),
            patch("reviewer.test_runner.run_tests", return_value={"status": "pass"}) as mock,
        ):
            result = run_tester(**self._kwargs, model_override="")
        mock.assert_called_once_with(
            repo_full_name="alice/backend", pr_number=7,
            head_sha="a" * 40, base_ref="main", head_ref="", model_override="", stack="",
        )
        assert result["status"] == "pass"

    def test_run_security_scanner_delegates(self):
        from event_bus.jobs.pr_jobs import run_security_scanner
        with (
            patch("event_bus.jobs.pr_jobs._redis_and_limits", return_value=_no_limits()),
            patch("event_bus.sandbox.get_sandbox", return_value=_direct_sandbox()),
            patch("reviewer.security_scan.run_security_scan", return_value={"status": "pass"}) as mock,
        ):
            result = run_security_scanner(**self._kwargs, model_override="")
        mock.assert_called_once_with(
            repo_full_name="alice/backend", pr_number=7,
            head_sha="a" * 40, base_ref="main", head_ref="", model_override="",
        )
        assert result["status"] == "pass"

    def test_insufficient_credits_alerts_operator(self):
        # A credit-exhaustion verdict surfaces a one-time operator alert and is returned
        # (not raised), so the watchdog doesn't loop on an unrunnable call.
        from event_bus.jobs.pr_jobs import run_code_reviewer
        credit_res = {"status": "error", "role": "code_review",
                      "reason": "insufficient_credits", "detail": "402", "operator_action": True}
        with (
            patch("event_bus.jobs.pr_jobs._redis_and_limits", return_value=_no_limits()),
            patch("event_bus.sandbox.get_sandbox", return_value=_direct_sandbox()),
            patch("reviewer.code_review.run_code_review", return_value=credit_res),
            patch("event_bus.jobs.pr_jobs._alert_operator_insufficient_credits") as mock_alert,
        ):
            result = run_code_reviewer(**self._kwargs, model_override="m")
        assert result["reason"] == "insufficient_credits"
        mock_alert.assert_called_once()

    def test_sandbox_crash_returns_error_not_raise(self):
        # A sandbox crash must be caught and returned as a structured error, not raised
        # uncaught (which would strand the PR with no verdict + an RQ retry storm).
        from event_bus.jobs.pr_jobs import run_code_reviewer
        sb = MagicMock()
        sb.run.side_effect = RuntimeError("container exited 1")
        with (
            patch("event_bus.jobs.pr_jobs._redis_and_limits", return_value=_no_limits()),
            patch("event_bus.sandbox.get_sandbox", return_value=sb),
            patch("reviewer.code_review.run_code_review", return_value={"status": "pass"}),
        ):
            result = run_code_reviewer(**self._kwargs, model_override="m")
        assert result["status"] == "error"
        assert result["role"] == "code_review"
        assert "reviewer_failed" in result["reason"]

    def test_missing_package_returns_error(self):
        from event_bus.jobs.pr_jobs import run_code_reviewer
        with (
            patch("event_bus.jobs.pr_jobs._redis_and_limits", return_value=_no_limits()),
            patch.dict("sys.modules", {"reviewer": None, "reviewer.code_review": None}),
        ):
            result = run_code_reviewer(**self._kwargs)
        assert result["status"] == "error"
        assert result["role"] == "code_review"


# ── handle_pr_event fan-out ──────────────────────────────────────────────────

def _make_queue():
    q = MagicMock()
    job1, job2, job3 = MagicMock(), MagicMock(), MagicMock()
    job1.id, job2.id, job3.id = "j1", "j2", "j3"
    q.enqueue.side_effect = [job1, job2, job3]
    return q


class TestHandlePrEvent:
    def test_enqueues_three_jobs(self):
        from event_bus.jobs.handlers import handle_pr_event
        q = _make_queue()
        r = fakeredis.FakeRedis()
        with (
            patch("redis.from_url", return_value=r),
            patch("rq.Queue", return_value=q),
        ):
            result = handle_pr_event(
                repo_full_name="alice/backend",
                pr_number=7,
                head_sha="a" * 40,
                action="opened",
                head_ref="feat/login",
                base_ref="main",
            )
        assert result["status"] == "dispatched"
        assert len(result["jobs"]) == 3
        assert q.enqueue.call_count == 3

    def test_passes_base_ref_to_jobs(self):
        from event_bus.jobs.handlers import handle_pr_event
        q = _make_queue()
        r = fakeredis.FakeRedis()
        with (
            patch("redis.from_url", return_value=r),
            patch("rq.Queue", return_value=q),
        ):
            handle_pr_event("alice/backend", 7, "a" * 40, "opened", "feat", "develop")

        for enqueue_call in q.enqueue.call_args_list:
            assert enqueue_call[1]["base_ref"] == "develop"

    def test_default_base_ref_is_main(self):
        from event_bus.jobs.handlers import handle_pr_event
        q = _make_queue()
        r = fakeredis.FakeRedis()
        with (
            patch("redis.from_url", return_value=r),
            patch("rq.Queue", return_value=q),
        ):
            handle_pr_event("alice/backend", 7, "a" * 40, "opened")

        for enqueue_call in q.enqueue.call_args_list:
            assert enqueue_call[1]["base_ref"] == "main"
