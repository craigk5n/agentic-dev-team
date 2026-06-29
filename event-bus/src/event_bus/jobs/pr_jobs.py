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

    try:
        from reviewer.code_review import run_code_review
        return get_sandbox().run(
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
    except ImportError:
        log.error("reviewer_not_installed")
        return {"status": "error", "role": "code_review", "reason": "reviewer package not available"}
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

    try:
        from reviewer.test_runner import run_tests
        return get_sandbox().run(
            run_tests,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            base_ref=base_ref,
            head_ref=head_ref,
            model_override=model_override,
            stack=stack,
        )
    except ImportError:
        log.error("reviewer_not_installed")
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

    try:
        from reviewer.security_scan import run_security_scan
        return get_sandbox().run(
            run_security_scan,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=head_sha,
            base_ref=base_ref,
            head_ref=head_ref,
            model_override=model_override,
        )
    except ImportError:
        log.error("reviewer_not_installed")
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
