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
) -> dict:
    """
    Decompose an approved idea into stories and return the plan.
    Persistence is handled by the caller (event-bus saves to SQLite).
    """
    log.info("planner_start", item_id=item_id)

    plan = decompose_idea(
        title,
        description,
        model=model_override or settings.model_planner,
        api_key=settings.effective_api_key,
        default_repo=settings.default_repo,
    )

    log.info(
        "planner_done",
        item_id=item_id,
        module=plan["module_name"],
        stories=len(plan.get("stories", [])),
    )
    return plan
