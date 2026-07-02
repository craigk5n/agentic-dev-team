"""Planner Agent — triggered when an idea is approved."""

from __future__ import annotations
import structlog

from planner_agent.config import settings
from planner_agent.decomposer import decompose_idea, normalize_plan

log = structlog.get_logger()


def run_import(
    item_id: str,
    plan_text: str,
    model_override: str = "",
    repo_full_name: str = "",
    stack: str = "",
    redis_conn=None,
    skip_epics: set[str] | None = None,
) -> dict:
    """Normalize an externally-authored plan into our epic/story model. Same return
    shape as run_planner; persistence is handled by the caller. ``skip_epics`` resumes a
    partial import by re-normalizing only the epics that don't have stories yet."""
    log.info("import_start", item_id=item_id, repo=repo_full_name or settings.default_repo,
             plan_chars=len(plan_text or ""), resume_skip=len(skip_epics or ()))
    plan = normalize_plan(
        plan_text,
        model=model_override or settings.model_planner,
        api_key=settings.effective_api_key,
        default_repo=repo_full_name or settings.default_repo,
        stack=stack,
        redis_conn=redis_conn,
        skip_epics=skip_epics,
    )
    log.info("import_done", item_id=item_id, epics=len(plan.get("epics", [])),
             stories=len(plan.get("stories", [])))
    return plan


def run_planner(
    item_id: str,
    title: str,
    description: str,
    model_override: str = "",
    repo_full_name: str = "",
    sdlc_directive: str = "",
    best_practices: str = "",
    stack: str = "",
    decisions: str = "",
    redis_conn=None,
) -> dict:
    """
    Decompose an approved idea into stories and return the plan.
    Persistence is handled by the caller (event-bus saves to SQLite).
    repo_full_name: the already-provisioned Forgejo repo (owner/name); if empty,
    falls back to settings.default_repo so existing behaviour is unchanged.
    sdlc_directive / best_practices: stack & SDLC guidance that shape decomposition.
    stack: stack id, recorded with telemetry so spend is attributed per stack.
    decisions: operator-locked design decisions injected as planning constraints.
    """
    log.info("planner_start", item_id=item_id, repo=repo_full_name or settings.default_repo)

    plan = decompose_idea(
        title,
        description,
        model=model_override or settings.model_planner,
        api_key=settings.effective_api_key,
        default_repo=repo_full_name or settings.default_repo,
        sdlc_directive=sdlc_directive,
        best_practices=best_practices,
        stack=stack,
        decisions=decisions,
        redis_conn=redis_conn,
    )

    log.info(
        "planner_done",
        item_id=item_id,
        module=plan["module_name"],
        stories=len(plan.get("stories", [])),
    )
    return plan
