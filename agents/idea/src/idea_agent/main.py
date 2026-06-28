"""
Idea Agent entry point.

Usage:
  python -m idea_agent.main "Add JWT authentication"
  # or called from the event-bus /api/ideas endpoint
"""

from __future__ import annotations
import structlog

from idea_agent.config import settings
from idea_agent.generator import expand_prompt

log = structlog.get_logger()


def expand_idea(
    prompt: str,
    model_override: str = "",
    redis_conn=None,
) -> dict:
    """
    Expand a prompt into a structured proposal dict.
    Returns {title, description} — persistence is handled by the caller.
    """
    log.info("idea_agent_start", prompt=prompt[:80])
    proposal = expand_prompt(
        prompt,
        model=model_override or settings.model_idea,
        api_key=settings.effective_api_key,
        redis_conn=redis_conn,
    )
    log.info("idea_agent_done", title=proposal.get("title", ""))
    return proposal


# Keep submit_idea as a thin shim so any existing callers don't break
def submit_idea(
    prompt: str,
    project_id: str = "",
    workspace_slug: str = "",
    model_override: str = "",
) -> dict:
    proposal = expand_idea(prompt, model_override=model_override)
    return {
        "status": "pending_approval",
        "title": proposal["title"],
        "proposal": proposal,
    }


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m idea_agent.main '<prompt>'")
        sys.exit(1)
    result = expand_idea(" ".join(sys.argv[1:]))
    print(f"Title: {result['title']}")
    print(f"Description:\n{result['description']}")
