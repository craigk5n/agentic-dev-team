"""
Job handlers.

Each function is a unit of work enqueued by the dispatcher and executed by the
RQ worker. Handlers read the runtime config from Redis on entry so model overrides,
gate flags, and rate/concurrency limits take effect on the next job.

Phase 7: rate limits are checked before enqueuing. Concurrency slots are managed
inside the individual job functions (pr_jobs.py) so limits apply to actual
execution, not queue depth.
"""

from __future__ import annotations
import structlog

log = structlog.get_logger()


def handle_pr_event(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    action: str,
    head_ref: str = "main",
    base_ref: str = "main",
) -> dict:
    """
    Triggered by Forgejo pull_request events.
    Fans out 3 independent review jobs with per-role model overrides from runtime config.
    The last job to finish posts the aggregated verdict and applies gate logic.

    Phase 7: rate-limit each role before enqueue.
    """
    import redis
    from rq import Queue
    from event_bus.config import settings
    from event_bus.config_store import get_config
    from event_bus.prompt_store import get_prompt
    from event_bus.jobs.pr_jobs import run_code_reviewer, run_tester, run_security_scanner
    from event_bus.limits import check_rate

    r = redis.from_url(settings.redis_url, decode_responses=False)
    config = get_config(r)

    # Cost backstop — don't fan out 3 LLM reviews if today's spend cap is reached
    from event_bus.cost_guard import over_budget
    if over_budget(r, config.limits.max_cost_usd_daily):
        log.warning("pr_event_cost_capped", repo=repo_full_name, pr=pr_number)
        return {"status": "cost_capped", "reason": "daily cost cap reached"}

    reviewer_system_prompt = get_prompt(r, "reviewer.system")
    reviewer_task_prompt = get_prompt(r, "reviewer.task")

    lim = config.limits
    rejected_roles = []

    # Rate check per role — a rejection here skips the job, not the whole PR
    reviewer_ok = check_rate(r, "reviewer", lim.max_rpm_reviewer)
    tester_ok = check_rate(r, "tester", lim.max_rpm_tester)
    security_ok = check_rate(r, "security", lim.max_rpm_security)

    if not (reviewer_ok or tester_ok or security_ok):
        # All roles are rate-limited — hard stop
        log.warning("pr_event_all_roles_rate_limited", repo=repo_full_name, pr=pr_number)
        return {"status": "rate_limited", "reason": "all review roles rate-limited"}

    for role, ok in [("reviewer", reviewer_ok), ("tester", tester_ok), ("security", security_ok)]:
        if not ok:
            rejected_roles.append(role)
            log.warning("pr_role_rate_limited", role=role, repo=repo_full_name, pr=pr_number)

    log.info("pr_event_fan_out", repo=repo_full_name, pr=pr_number, sha=head_sha[:8],
             base=base_ref, model_reviewer=config.models.reviewer or "default",
             model_tester=config.models.tester or "default",
             model_security=config.models.security or "default",
             rejected=rejected_roles)

    q = Queue("agent-jobs", connection=r)
    base_kwargs = dict(repo_full_name=repo_full_name, pr_number=pr_number,
                       head_sha=head_sha, base_ref=base_ref, head_ref=head_ref)
    jobs = []
    if reviewer_ok:
        jobs.append(q.enqueue(run_code_reviewer, **base_kwargs,
                              model_override=config.models.reviewer,
                              system_prompt=reviewer_system_prompt,
                              task_prompt=reviewer_task_prompt))
    if tester_ok:
        jobs.append(q.enqueue(run_tester, **base_kwargs,
                              model_override=config.models.tester))
    if security_ok:
        jobs.append(q.enqueue(run_security_scanner, **base_kwargs,
                              model_override=config.models.security))

    job_ids = [j.id for j in jobs]
    log.info("pr_jobs_enqueued", repo=repo_full_name, pr=pr_number, job_ids=job_ids)
    return {"status": "dispatched", "jobs": job_ids, "rate_limited": rejected_roles}
