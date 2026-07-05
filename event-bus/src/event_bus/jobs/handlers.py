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
    from rq import Queue, Retry
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

    # Resolve the repo's stack once: used to (a) make the reviewer stack-aware (5.4)
    # and (b) attribute LLM spend to the stack in telemetry (6.2).
    stack_id = ""
    try:
        from event_bus.main import _stack_id_for_repo
        from event_bus.catalog import get_catalog
        owner, repo = repo_full_name.split("/", 1)
        cat = get_catalog()
        stack = cat.get_stack(_stack_id_for_repo(owner, repo))
        stack_id = stack.id
        if stack.best_practices_prompt.strip():
            reviewer_task_prompt += (
                "\n\nStack conventions to check (this is a "
                f"{stack.display_name} project):\n" + stack.best_practices_prompt.strip()
            )
        if getattr(stack, "security_checklist", "").strip():
            reviewer_task_prompt += (
                "\n\nSecurity requirements to enforce (flag any violation as a blocking "
                "finding):\n" + stack.security_checklist.strip()
            )
        # HS-7: verify the project's NFRs (local-first / offline-capable) — the assertions
        # are reconciled once at approval and enforced on every PR, not re-decided per story.
        from event_bus import nfrs as nfr_catalog
        from event_bus.work_store import get_nfrs_for_repo
        nfr_assertions = nfr_catalog.assertions(get_nfrs_for_repo(repo_full_name))
        if nfr_assertions:
            reviewer_task_prompt += "\n\n" + nfr_assertions
        # Make the reviewer check adherence to the project's chosen style guides.
        from event_bus.work_store import get_style_guides_for_repo
        for g in cat.get_style_guides(get_style_guides_for_repo(repo_full_name)):
            if g.prompt.strip():
                reviewer_task_prompt += (
                    f"\n\nStyle guide to check — {g.display_name}:\n" + g.prompt.strip()
                )
    except Exception as exc:
        log.warning("reviewer_stack_resolve_skipped", error=str(exc))

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

    # HS-9: resolve the story this PR belongs to so verdict spend is attributed per-story
    # (summed with the coder spend). Best-effort — "" if not found yet.
    story_id = ""
    try:
        from event_bus.work_store import find_story_id_by_pr
        story_id = find_story_id_by_pr(repo_full_name, pr_number)
    except Exception as exc:
        log.debug("pr_story_lookup_failed", error=str(exc)[:120])

    q = Queue("agent-jobs", connection=r)
    base_kwargs = dict(repo_full_name=repo_full_name, pr_number=pr_number,
                       head_sha=head_sha, base_ref=base_ref, head_ref=head_ref)
    # Retry each verdict job on failure: the agents call flaky free models and hit a
    # sandboxed container, so a transient crash shouldn't permanently strand the PR with
    # a missing verdict (no verdict = never merges). Backoff gives the provider time.
    retry = Retry(max=2, interval=[20, 60])
    # If this PR's story has been auto-escalated, review it with the stronger model too
    # (matching the escalated coder) — otherwise a strong coder vs weak reviewer just
    # re-deadlocks. Set by internal_recode_for_pr when the recode cap is exhausted.
    esc = r.get(f"escalate_pr:{repo_full_name}:{pr_number}")
    reviewer_model = (esc.decode() if esc else "") or config.models.reviewer
    # Rate-limit circuit breaker: if any verdict model is paused (429), HOLD the whole
    # fan-out rather than enqueue a partial set (which would never aggregate). The story
    # stays in-review and the watchdog re-queues once the breaker clears — no false stuck.
    from event_bus import ratelimit
    if any(ratelimit.is_tripped(r, vm)
           for vm in (reviewer_model, config.models.tester, config.models.security) if vm):
        log.warning("pr_event_rate_limit_hold", repo=repo_full_name, pr=pr_number)
        return {"status": "rate_limit_hold", "reason": "a verdict model is rate-limited"}
    jobs = []
    if reviewer_ok:
        # A subscription (claude-code) reviewer shells to the claude CLI, which reviews a
        # full diff agentically (up to ~5 min). RQ's 180s default would kill it mid-review
        # and strand the verdict — give the reviewer job real headroom (litellm reviews
        # finish well within it, so this doesn't slow the common case).
        jobs.append(q.enqueue(run_code_reviewer, **base_kwargs,
                              model_override=reviewer_model,
                              system_prompt=reviewer_system_prompt,
                              task_prompt=reviewer_task_prompt,
                              stack=stack_id, story_id=story_id, retry=retry, job_timeout=420))
    if tester_ok:
        jobs.append(q.enqueue(run_tester, **base_kwargs,
                              model_override=config.models.tester,
                              stack=stack_id, story_id=story_id, retry=retry))
    if security_ok:
        jobs.append(q.enqueue(run_security_scanner, **base_kwargs,
                              model_override=config.models.security, retry=retry))

    job_ids = [j.id for j in jobs]
    log.info("pr_jobs_enqueued", repo=repo_full_name, pr=pr_number, job_ids=job_ids)
    return {"status": "dispatched", "jobs": job_ids, "rate_limited": rejected_roles}
