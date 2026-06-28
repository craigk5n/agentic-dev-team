"""Planner Agent — triggered when an idea is approved."""

from __future__ import annotations
import structlog

from planner_agent.config import settings
from planner_agent.decomposer import decompose_idea

log = structlog.get_logger()


def run_planner(
    item_id: str,
    title: str,
    description: str,
    model_override: str = "",
    repo_full_name: str = "",
    redis_conn=None,
) -> dict:
    """
    Decompose an approved idea into stories and return the plan.
    Persistence is handled by the caller (event-bus saves to SQLite).
    repo_full_name: the already-provisioned Forgejo repo (owner/name); if empty,
    falls back to settings.default_repo so existing behaviour is unchanged.
    """
    log.info("planner_start", item_id=item_id, repo=repo_full_name or settings.default_repo)

    plan = decompose_idea(
        title,
        description,
        model=model_override or settings.model_planner,
        api_key=settings.effective_api_key,
        default_repo=repo_full_name or settings.default_repo,
        redis_conn=redis_conn,
    )

    log.info(
        "planner_done",
        item_id=item_id,
        module=plan["module_name"],
        stories=len(plan.get("stories", [])),
    )
    return plan
