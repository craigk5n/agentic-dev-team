"""
RQ job wrappers for PR review fan-out.

Phase 6: model_override + merge.
Phase 7: concurrency slot management + sandbox execution.

Each function acquires a concurrency slot (from LimitsConfig), runs the agent
through the Sandbox (in-process or Docker, per SANDBOX_MODE), and releases the
slot in a finally block.
"""

from __future__ import annotations
import structlog

log = structlog.get_logger()


def _redis_and_limits():
    """Return (redis_conn, LimitsConfig) from the runtime config. Never raises."""
    try:
        import redis
        from event_bus.config import settings
        from event_bus.config_store import get_config
        r = redis.from_url(settings.redis_url, decode_responses=False)
        config = get_config(r)
        return r, config.limits
    except Exception as exc:
        log.warning("limits_config_unavailable", error=str(exc))
        from event_bus.config_store import LimitsConfig
        return None, LimitsConfig()


_VERDICT_CHECK = {"reviewer": "code_review", "tester": "test_run", "security": "security"}


def _verdict_status(r, repo_full_name: str, pr_number: int, check: str) -> str:
    """Current stored verdict status for a PR+check, or '' — used to detect flips."""
    try:
        import json
        owner, repo = repo_full_name.split("/", 1)
        raw = r.get(f"pr_verdict:{owner}:{repo}:{pr_number}:{check}")
        if raw:
            return json.loads(raw).get("status", "")
    except Exception:
        pass
    return ""


def _looks_rate_limited(result: dict) -> bool:
    t = (str((result or {}).get("reason", "")) + str((result or {}).get("error", ""))).lower()
    return "429" in t or "rate limit" in t or "rate_limit" in t or "ratelimit" in t


def _alert_operator_insufficient_credits(r, repo_full_name: str, pr_number: int,
                                         model: str, detail: str) -> None:
    """Post a one-time operator comment when a verdict can't run for lack of provider
    credit. Guarded by a Redis key (6h TTL) so the watchdog's periodic re-runs don't
    spam the PR. Best-effort — never raises into the job."""
    try:
        key = f"credit_alert:{repo_full_name}:{pr_number}"
        if r is not None and not r.set(key, "1", nx=True, ex=6 * 3600):
            return  # already alerted recently
        from event_bus.config import settings
        from coding_agent.forgejo_client import ForgejoClient
        owner, repo = repo_full_name.split("/", 1)
        body = (
            "⛔ **Review paused — out of model credits.**\n\n"
            f"The reviewer (`{model or 'default'}`) could not run: {detail or 'insufficient credit/quota'}.\n\n"
            "This is an operator action, not a code issue. Top up the provider balance "
            "(or switch this role to a cheaper/free model), then **Retry** this story."
        )
        with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as fj:
            fj.post_pr_comment(owner, repo, pr_number, body)
        log.warning("operator_alert_insufficient_credits", repo=repo_full_name, pr=pr_number)
    except Exception as exc:
        log.debug("credit_alert_skipped", error=str(exc)[:120])


def _capture_verdict(r, role: str, model: str, prior: str, result: dict, ms) -> None:
    """Record a verdict's Metrics signals: reliability (pass/fail vs error/rate_limited),
    a flip when a re-review changed the verdict, and call latency. Best-effort."""
    try:
        from event_bus.outcomes import record_outcome, record_latency
        from event_bus import ratelimit
        m = model or "(default)"
        status = (result or {}).get("status", "")
        if status in ("pass", "fail"):
            outcome = status
        elif _looks_rate_limited(result):
            outcome = "rate_limited"
        else:
            outcome = "error"
        record_outcome(r, role, m, outcome)
        # Feed the rate-limit circuit breaker: a 429 arms it, a real verdict clears it.
        if outcome == "rate_limited" and model:
            ratelimit.trip(r, model)
        elif outcome in ("pass", "fail") and model:
            ratelimit.clear(r, model)
        if prior in ("pass", "fail") and status in ("pass", "fail") and prior != status:
            record_outcome(r, role, m, "flip")   # a re-review changed its mind
        if ms is not None:
            record_latency(r, role, m, ms)
    except Exception:
        pass


def run_code_reviewer(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    base_ref: str = "main",
    head_ref: str = "",
    model_override: str = "",
    system_prompt: str = "",
    task_prompt: str = "",
    stack: str = "",
) -> dict:
    r, lim = _redis_and_limits()
    from event_bus.limits import acquire_slot, release_slot
    from event_bus.sandbox import get_sandbox

    if not acquire_slot(r, "reviewer", lim.max_concurrent_reviewer):
        return {"status": "error", "role": "code_review", "reason": "concurrency_limit_exceeded"}

    import time as _t
    prior = _verdict_status(r, repo_full_name, pr_number, "code_review")
    t0 = _t.monotonic()
    try:
        from reviewer.code_review import run_code_review
        res = get_sandbox().run(
            run_code_review,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            base_ref=base_ref,
            head_ref=head_ref,
            model_override=model_override,
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            stack=stack,
        )
        _capture_verdict(r, "reviewer", model_override, prior, res, (_t.monotonic() - t0) * 1000)
        # Out-of-credit is unrecoverable — surface it to the operator once (guarded so the
        # watchdog's re-runs don't spam the PR) rather than silently looping forever.
        if isinstance(res, dict) and res.get("reason") == "insufficient_credits":
            _alert_operator_insufficient_credits(r, repo_full_name, pr_number, model_override,
                                                 res.get("detail", ""))
        return res
    except ImportError:
        log.error("reviewer_not_installed")
        _capture_verdict(r, "reviewer", model_override, prior, {"status": "error"}, None)
        return {"status": "error", "role": "code_review", "reason": "reviewer package not available"}
    except Exception as exc:
        # A sandbox crash (ContainerError, OOM, etc.) must not raise uncaught — that strands
        # the PR with no verdict and no signal. Record an error outcome and return a
        # structured error so the caller/watchdog can see it instead of an RQ retry storm.
        log.error("reviewer_job_failed", repo=repo_full_name, pr=pr_number, error=str(exc)[:300])
        _capture_verdict(r, "reviewer", model_override, prior, {"status": "error"}, None)
        return {"status": "error", "role": "code_review", "reason": f"reviewer_failed: {str(exc)[:200]}"}
    finally:
        release_slot(r, "reviewer")


def run_tester(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    base_ref: str = "main",
    head_ref: str = "",
    model_override: str = "",
    stack: str = "",
) -> dict:
    r, lim = _redis_and_limits()
    from event_bus.limits import acquire_slot, release_slot
    from event_bus.sandbox import get_sandbox

    if not acquire_slot(r, "tester", lim.max_concurrent_tester):
        return {"status": "error", "role": "test_run", "reason": "concurrency_limit_exceeded"}

    import time as _t
    prior = _verdict_status(r, repo_full_name, pr_number, "test_run")
    t0 = _t.monotonic()
    try:
        from reviewer.test_runner import run_tests
        res = get_sandbox().run(
            run_tests,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            base_ref=base_ref,
            head_ref=head_ref,
            model_override=model_override,
            stack=stack,
        )
        _capture_verdict(r, "tester", model_override, prior, res, (_t.monotonic() - t0) * 1000)
        return res
    except ImportError:
        log.error("reviewer_not_installed")
        _capture_verdict(r, "tester", model_override, prior, {"status": "error"}, None)
        return {"status": "error", "role": "test_run", "reason": "reviewer package not available"}
    finally:
        release_slot(r, "tester")


def run_security_scanner(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    base_ref: str = "main",
    head_ref: str = "",
    model_override: str = "",
) -> dict:
    r, lim = _redis_and_limits()
    from event_bus.limits import acquire_slot, release_slot
    from event_bus.sandbox import get_sandbox

    if not acquire_slot(r, "security", lim.max_concurrent_security):
        return {"status": "error", "role": "security", "reason": "concurrency_limit_exceeded"}

    import time as _t
    prior = _verdict_status(r, repo_full_name, pr_number, "security")
    t0 = _t.monotonic()
    try:
        from reviewer.security_scan import run_security_scan
        res = get_sandbox().run(
            run_security_scan,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            base_ref=base_ref,
            head_ref=head_ref,
            model_override=model_override,
        )
        _capture_verdict(r, "security", model_override, prior, res, (_t.monotonic() - t0) * 1000)
        return res
    except ImportError:
        log.error("reviewer_not_installed")
        _capture_verdict(r, "security", model_override, prior, {"status": "error"}, None)
        return {"status": "error", "role": "security", "reason": "reviewer package not available"}
    finally:
        release_slot(r, "security")


def do_merge_pr(owner: str, repo: str, pr_number: int, approver: str = "human") -> dict:
    """
    Merge a PR via Forgejo API. Called when a human approves via POST /api/prs/.../approve
    and Temporal is not configured (RQ fallback path).
    """
    try:
        from reviewer.forgejo_client import ForgejoClient
        from reviewer.config import settings
        with ForgejoClient(settings.forgejo_base_url, settings.forgejo_api_token) as fj:
            result = fj.merge_pr(owner, repo, pr_number)
        log.info("pr_merged_on_approval", repo=f"{owner}/{repo}", pr=pr_number, approver=approver)
        return {"status": "merged", "approver": approver, **result}
    except ImportError:
        log.error("reviewer_not_installed")
        return {"status": "error", "reason": "reviewer package not available"}
    except Exception as exc:
        log.error("merge_failed", repo=f"{owner}/{repo}", pr=pr_number, error=str(exc))
        return {"status": "error", "reason": str(exc)}
