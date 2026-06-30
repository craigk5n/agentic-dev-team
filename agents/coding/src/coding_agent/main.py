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
import shlex
import shutil
import subprocess
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

# In-coder TDD: how many times the coder re-invokes opencode after a failing
# test run before giving up and opening the PR anyway (CI/reviewer still gate).
_MAX_TEST_ITERS = 2
_TEST_TIMEOUT = 300  # seconds


def _run_install(
    repo_dir: str,
    command: str,
    log_line: Callable[[str], None] | None = None,
    timeout: int = _TEST_TIMEOUT,
) -> None:
    """Best-effort install of the project + deps before in-coder tests. Never raises."""
    if not command.strip():
        return
    try:
        proc = subprocess.run(
            command, cwd=repo_dir, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("install_timed_out")
        return
    if proc.returncode != 0:
        log.info("install_command_nonzero", rc=proc.returncode)
        if log_line:
            for ln in (proc.stdout + proc.stderr).splitlines()[-10:]:
                log_line(ln)


def _run_test_command(
    repo_dir: str,
    command: str,
    log_line: Callable[[str], None] | None = None,
    timeout: int = _TEST_TIMEOUT,
) -> tuple[str, str]:
    """Run the stack's test command in repo_dir.

    Returns (status, output) where status is:
      'pass'    — tests ran and succeeded
      'fail'    — tests ran and failed
      'skipped' — the toolchain isn't available in this sandbox image (no-op)
    """
    if not command.strip():
        return ("skipped", "")
    primary = shlex.split(command)[0]
    if shutil.which(primary) is None:
        log.info("test_skipped_no_toolchain", primary=primary)
        return ("skipped", f"{primary}: not found in sandbox image")
    try:
        proc = subprocess.run(
            command, cwd=repo_dir, shell=True,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ("fail", f"test command timed out after {timeout}s")
    out = proc.stdout + proc.stderr
    if log_line:
        for ln in out.splitlines()[-40:]:
            log_line(ln)
    # A failure caused by missing test tooling (not by the code) → skip, don't iterate.
    if proc.returncode != 0 and re.search(
        r"No module named|not installed|cannot find package|No such file or directory", out
    ):
        log.info("test_skipped_tooling_missing", primary=primary)
        return ("skipped", out)
    return ("pass" if proc.returncode == 0 else "fail", out)


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
    test_command: str = "",
    install_command: str = "",
    log_line: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Run the full coding agent loop for one story.
    Returns a result dict; raises on unrecoverable errors.
    State transitions are handled by the event-bus caller.

    test_command: when set, the coder runs it in-sandbox after writing code and
    re-attempts (red->green) until it passes or _MAX_TEST_ITERS is reached, before
    opening the PR. Skipped gracefully if the toolchain isn't in the sandbox image.
    install_command: run before each test attempt to install the project + its
    dependencies, so in-coder tests can import third-party packages.
    """
    log.info("coding_agent_start", item_id=item_id, title=title)

    repo_full = _extract_repo(description) or settings.default_repo
    owner, repo_name = repo_full.split("/", 1)
    branch = _branch_name(item_id, title)
    log.info("target_repo", repo=repo_full, branch=branch)

    with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as forgejo:
        with tempfile.TemporaryDirectory(prefix="coding-agent-") as tmpdir:
            try:
                git_ops.clone(
                    clone_base_url=settings.forgejo_clone_base,
                    owner=owner,
                    repo=repo_name,
                    api_token=settings.effective_forgejo_token,
                    user=settings.effective_forgejo_user,
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

            # In-coder TDD: run the stack's tests and iterate (red->green) before the PR.
            test_status = "skipped"
            if test_command:
                for attempt in range(_MAX_TEST_ITERS + 1):
                    # Install the project + deps first so tests can import them
                    # (re-run each attempt in case the agent added a dependency).
                    _run_install(tmpdir, install_command, log_line)
                    test_status, test_output = _run_test_command(tmpdir, test_command, log_line)
                    if test_status in ("pass", "skipped"):
                        break
                    if attempt >= _MAX_TEST_ITERS:
                        log.warning("tests_still_failing", item_id=item_id, attempts=attempt + 1)
                        break
                    if log_line:
                        log_line(f"[tdd] tests failing — re-attempt {attempt + 1}/{_MAX_TEST_ITERS}")
                    summary = run_opencode_agent(
                        story_title=title,
                        story_description=description,
                        repo_dir=tmpdir,
                        model=model,
                        openrouter_api_key=settings.openrouter_api_key,
                        review_comments=[{
                            "body": f"The test suite is failing. Fix the code so `{test_command}` "
                                    f"passes.\n\nTest output:\n{test_output[-3000:]}"
                        }],
                        prompt_template=story_prompt,
                        log_line=log_line,
                    )
                log.info("in_coder_tests", item_id=item_id, status=test_status)

            sha = git_ops.commit_all(tmpdir, f"feat: {title}\n\n{summary}")
            if not sha:
                log.warning("no_changes_committed", item_id=item_id)
                return {"status": "no_changes", "item_id": item_id, "summary": summary}

            git_ops.push(tmpdir, branch)

        _test_line = {
            "pass": "🧪 In-coder tests: **passing**",
            "fail": "🧪 In-coder tests: **failing** (CI/reviewer will gate)",
            "skipped": "",
        }.get(test_status, "")
        pr_body = (
            f"## Story\n{title}\n\n"
            f"## Description\n{description or '(none)'}\n\n"
            f"## Implementation summary\n{summary}\n\n"
            + (f"{_test_line}\n\n" if _test_line else "")
            + f"---\n_Work item: `{item_id}`_"
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
        "test_status": test_status,
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

    with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as forgejo:
        with tempfile.TemporaryDirectory(prefix="coding-agent-fix-") as tmpdir:
            git_ops.clone(
                clone_base_url=settings.forgejo_clone_base,
                owner=owner,
                repo=repo_name,
                api_token=settings.effective_forgejo_token,
                user=settings.effective_forgejo_user,
                target_dir=tmpdir,
            )
            git_ops.configure_identity(tmpdir, settings.git_author_name, settings.git_author_email)
            git_ops.checkout_branch(tmpdir, branch)

            # Bring the branch up to date with base so a stale/conflicting PR can
            # merge. A clean merge advances the branch; a conflict leaves markers for
            # the agent to resolve below.
            merge_clean = git_ops.merge_base_branch(tmpdir, base="main")
            if not merge_clean and "conflict" not in (review_comments[-1].get("body", "").lower()
                                                       if review_comments else ""):
                review_comments = review_comments + [{
                    "path": "",
                    "body": "Resolve the git merge conflicts with `main`: remove all conflict "
                            "markers (<<<<<<<, =======, >>>>>>>) and keep a correct, working "
                            "combination of both sides.",
                }]

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
            # Push if the agent changed anything OR the merge advanced the branch.
            if not sha and not git_ops.has_unpushed(tmpdir, branch):
                log.warning("recode_no_changes", item_id=item_id)
                return {"status": "no_changes", "item_id": item_id, "summary": summary}

            git_ops.push(tmpdir, branch)
            sha = sha or git_ops._run(["git", "rev-parse", "HEAD"], cwd=tmpdir)

    log.info("recode_agent_done", item_id=item_id, sha=sha[:8])
    return {"status": "success", "item_id": item_id, "sha": sha, "summary": summary}
