"""
Coding Agent orchestrator.

Flow:
  1. Receive story (id, title, description) from the event-bus
  2. Clone target repo from Forgejo
  3. Create feature branch
  4. Run Claude agentic loop
  5. Commit + push
  6. Open PR on Forgejo
  7. Return {status, pr_url, sha, summary} — state updates handled by caller
"""

from __future__ import annotations
import re
import tempfile
from typing import Any

import structlog

from coding_agent.config import settings
from coding_agent.forgejo_client import ForgejoClient
from coding_agent import git_ops
from coding_agent.claude_agent import run_agent

log = structlog.get_logger()


def _branch_name(item_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    short_id = item_id[:8]
    return f"story-{short_id}/{slug}"


def _extract_repo(description: str | None) -> str | None:
    """Parse optional 'repo: owner/name' directive from story description."""
    if not description:
        return None
    m = re.search(r"(?m)^repo:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", description)
    return m.group(1) if m else None


def run_coding_agent(
    item_id: str,
    title: str,
    description: str,
    model_override: str = "",
) -> dict[str, Any]:
    """
    Run the full coding agent loop for one story.
    Returns a result dict; raises on unrecoverable errors.
    State transitions are handled by the event-bus caller.
    """
    log.info("coding_agent_start", item_id=item_id, title=title)

    repo_full = _extract_repo(description) or settings.default_repo
    owner, repo_name = repo_full.split("/", 1)
    branch = _branch_name(item_id, title)
    log.info("target_repo", repo=repo_full, branch=branch)

    with ForgejoClient(settings.forgejo_base_url, settings.forgejo_api_token) as forgejo:
        with tempfile.TemporaryDirectory(prefix="coding-agent-") as tmpdir:
            try:
                git_ops.clone(
                    clone_base_url=settings.forgejo_clone_base,
                    owner=owner,
                    repo=repo_name,
                    api_token=settings.forgejo_api_token,
                    target_dir=tmpdir,
                )
            except RuntimeError as exc:
                raise RuntimeError(f"Clone failed: {exc}") from exc

            git_ops.configure_identity(tmpdir, settings.git_author_name, settings.git_author_email)
            git_ops.create_branch(tmpdir, branch)

            model = model_override or settings.anthropic_model
            summary = run_agent(
                story_title=title,
                story_description=description,
                repo_dir=tmpdir,
                api_key=settings.anthropic_api_key,
                model=model,
                max_tokens=settings.anthropic_max_tokens,
            )

            sha = git_ops.commit_all(tmpdir, f"feat: {title}\n\n{summary}")
            if not sha:
                log.warning("no_changes_committed", item_id=item_id)
                return {"status": "no_changes", "item_id": item_id, "summary": summary}

            git_ops.push(tmpdir, branch)

        pr_body = (
            f"## Story\n{title}\n\n"
            f"## Description\n{description or '(none)'}\n\n"
            f"## Implementation summary\n{summary}\n\n"
            f"---\n_Work item: `{item_id}`_"
        )
        pr = forgejo.create_pr(
            owner=owner,
            repo=repo_name,
            title=f"[{item_id[:8]}] {title}",
            body=pr_body,
            head=branch,
        )
        pr_url = pr.get("html_url", "")

    log.info("coding_agent_done", item_id=item_id, pr=pr_url, sha=sha[:8])
    return {
        "status": "success",
        "item_id": item_id,
        "pr_url": pr_url,
        "sha": sha,
        "summary": summary,
    }
