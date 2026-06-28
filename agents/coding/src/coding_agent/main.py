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
from collections.abc import Callable
from typing import Any

import structlog

from coding_agent.config import settings
from coding_agent.forgejo_client import ForgejoClient
from coding_agent import git_ops
from coding_agent.opencode_agent import run_opencode_agent
from typing import Any

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
    story_prompt: str = "",
    log_line: Callable[[str], None] | None = None,
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

            model = model_override or settings.model_coder
            summary = run_opencode_agent(
                story_title=title,
                story_description=description,
                repo_dir=tmpdir,
                model=model,
                openrouter_api_key=settings.openrouter_api_key,
                prompt_template=story_prompt,
                log_line=log_line,
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
        pr_number = pr.get("number")

        # Post the full agent output as a collapsible comment so it's visible in Forgejo
        if pr_number and summary:
            truncated = summary[-6000:]  # Forgejo comment size limit
            agent_comment = (
                "<details>\n<summary>🤖 Agent output</summary>\n\n"
                f"```\n{truncated}\n```\n\n</details>"
            )
            try:
                forgejo.post_pr_comment(owner, repo_name, pr_number, agent_comment)
            except Exception as exc:
                log.warning("agent_comment_failed", pr=pr_number, error=str(exc))

    log.info("coding_agent_done", item_id=item_id, pr=pr_url, sha=sha[:8])
    return {
        "status": "success",
        "item_id": item_id,
        "pr_url": pr_url,
        "sha": sha,
        "summary": summary,
    }


def fix_pr_review(
    item_id: str,
    title: str,
    description: str,
    branch: str,
    repo_full_name: str,
    review_comments: list[dict[str, Any]],
    model_override: str = "",
    review_fix_prompt: str = "",
    log_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Push a fix commit to an existing PR branch addressing review feedback.
    Returns {status, item_id, sha, summary} — does not open a new PR.
    """
    log.info("recode_agent_start", item_id=item_id, branch=branch, comments=len(review_comments))
    owner, repo_name = repo_full_name.split("/", 1)

    with ForgejoClient(settings.forgejo_base_url, settings.forgejo_api_token) as forgejo:
        with tempfile.TemporaryDirectory(prefix="coding-agent-fix-") as tmpdir:
            git_ops.clone(
                clone_base_url=settings.forgejo_clone_base,
                owner=owner,
                repo=repo_name,
                api_token=settings.forgejo_api_token,
                target_dir=tmpdir,
            )
            git_ops.configure_identity(tmpdir, settings.git_author_name, settings.git_author_email)
            git_ops.checkout_branch(tmpdir, branch)

            model = model_override or settings.model_coder
            summary = run_opencode_agent(
                story_title=title,
                story_description=description,
                repo_dir=tmpdir,
                model=model,
                openrouter_api_key=settings.openrouter_api_key,
                review_comments=review_comments,
                prompt_template=review_fix_prompt,
                log_line=log_line,
            )

            sha = git_ops.commit_all(tmpdir, f"fix: address review comments\n\n{summary[:500]}")
            if not sha:
                log.warning("recode_no_changes", item_id=item_id)
                return {"status": "no_changes", "item_id": item_id, "summary": summary}

            git_ops.push(tmpdir, branch)

    log.info("recode_agent_done", item_id=item_id, sha=sha[:8])
    return {"status": "success", "item_id": item_id, "sha": sha, "summary": summary}
