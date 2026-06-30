"""
Merge gate — runs after all 3 PR review verdicts arrive.

Checks gate flags from the shared Redis runtime config, then either blocks the
merge, holds it for human approval, or merges automatically.

Redis key layout (shared with event-bus config_store):
  runtime_config           → JSON with {gates: {security_signoff, pr_merge_approval}}
  pr_merge_pending:{o}:{r}:{n} → written here when awaiting approval; read + deleted
                                  by event-bus POST /api/prs/.../approve
"""

from __future__ import annotations
import json
import time
from typing import TYPE_CHECKING

import structlog

from reviewer.config import settings
from reviewer.forgejo_client import ForgejoClient

if TYPE_CHECKING:
    import redis as redis_module

log = structlog.get_logger()

_CONFIG_KEY = "runtime_config"
_PENDING_PREFIX = "pr_merge_pending:"


def _notify_merged(repo_full_name: str, pr_number: int, pr_url: str) -> None:
    """Tell the event-bus the PR was merged so it advances the story state."""
    import httpx
    url = f"{settings.event_bus_internal_url}/internal/pr-merged"
    try:
        resp = httpx.post(url, json={
            "repo_full_name": repo_full_name,
            "pr_number": pr_number,
            "pr_url": pr_url,
        }, timeout=10)
        resp.raise_for_status()
        log.info("pr_merge_notified", repo=repo_full_name, pr=pr_number)
    except Exception as exc:
        log.error("pr_merge_notify_failed", repo=repo_full_name, pr=pr_number, error=str(exc))


def _trigger_recode(
    repo_full_name: str, pr_number: int, pr_url: str, head_ref: str, feedback: str
) -> None:
    """Call the event-bus internal endpoint to start the recode agent."""
    import httpx
    url = f"{settings.event_bus_internal_url}/internal/recode-for-pr"
    try:
        resp = httpx.post(url, json={
            "repo_full_name": repo_full_name,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "head_ref": head_ref,
            "feedback": feedback,
        }, timeout=10)
        resp.raise_for_status()
        log.info("recode_triggered", repo=repo_full_name, pr=pr_number)
    except Exception as exc:
        log.error("recode_trigger_failed", repo=repo_full_name, pr=pr_number, error=str(exc))


def _clear_verdicts(r: "redis_module.Redis", repo_full_name: str, pr_number: int) -> None:
    """Delete all per-role verdict keys so the next push triggers a fresh review round."""
    owner, repo = repo_full_name.split("/", 1)
    base = f"pr_verdict:{owner}:{repo}:{pr_number}"
    keys = [f"{base}:code_review", f"{base}:test_run", f"{base}:security",
            f"{base}:aggregated"]
    existing = [k for k in keys if r.exists(k)]
    if existing:
        r.delete(*existing)


def _read_gates(r: "redis_module.Redis") -> dict:
    data = r.get(_CONFIG_KEY)
    if not data:
        return {"security_signoff": True, "pr_merge_approval": False}
    try:
        return json.loads(data).get("gates", {"security_signoff": True, "pr_merge_approval": False})
    except Exception:
        return {"security_signoff": True, "pr_merge_approval": False}


def apply_gate(
    r: "redis_module.Redis",
    repo_full_name: str,
    pr_number: int,
    all_verdicts: dict,
) -> dict:
    """
    Apply merge gate logic after all 3 review verdicts are in.
    Posts a follow-up PR comment/review and returns the gate outcome.
    """
    owner, repo = repo_full_name.split("/", 1)
    gates = _read_gates(r)

    security_status = all_verdicts.get("security", {}).get("status", "warn")

    # Gate 0: non-security checks failed — post a comment and directly call the
    # event-bus recode endpoint.  Security failures are handled by Gate 1 below
    # (security_signoff) so that a human can review them rather than auto-recoding.
    # Forgejo's pull_request_review_rejected webhook doesn't fire reliably for
    # API-submitted reviews, so we bypass the webhook chain entirely.
    failing = [
        role for role, v in all_verdicts.items()
        if role != "security" and v.get("status") == "fail"
    ]
    if failing:
        body = (
            "❌ **Changes required** — the following checks failed: "
            + ", ".join(failing)
            + ".\n\nThe coding agent will push a fix automatically."
        )
        with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as fj:
            fj.post_pr_comment(owner, repo, pr_number, body)
            pr_data = fj.get_pr(owner, repo, pr_number)
        head_ref = pr_data.get("head", {}).get("ref", "")
        pr_url = pr_data.get("html_url", "")
        _clear_verdicts(r, repo_full_name, pr_number)
        _trigger_recode(repo_full_name, pr_number, pr_url, head_ref, body)
        log.info("gate_changes_requested", repo=repo_full_name, pr=pr_number, failing=failing)
        return {"gate_status": "changes_requested", "failing": failing}

    # Gate 1: security signoff — block merge on security warnings too if flag set
    if gates.get("security_signoff", True) and security_status == "fail":
        msg = (
            "⛔ **Merge blocked** — security scan failed.\n\n"
            "Resolve the findings above and push a new commit to re-run checks."
        )
        with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as fj:
            fj.post_pr_comment(owner, repo, pr_number, msg)
        log.info("gate_blocked_security", repo=repo_full_name, pr=pr_number)
        return {"gate_status": "blocked", "reason": "security_fail"}

    # Gate 2: human PR merge approval
    if gates.get("pr_merge_approval", False):
        pending_key = f"{_PENDING_PREFIX}{owner}:{repo}:{pr_number}"
        r.setex(pending_key, 86400, json.dumps({
            "repo_full_name": repo_full_name,
            "pr_number": pr_number,
        }))
        msg = (
            "🔔 **Awaiting human approval to merge.**\n\n"
            "All automated checks passed. Approve via the API:\n"
            "```bash\n"
            f"curl -X POST http://<event-bus>/api/prs/{owner}/{repo}/{pr_number}/approve\n"
            "```\n"
            "Or set `gate.pr_merge_approval = false` in `/api/config` for auto-merge."
        )
        with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as fj:
            fj.post_pr_comment(owner, repo, pr_number, msg)
        log.info("gate_awaiting_approval", repo=repo_full_name, pr=pr_number)
        return {"gate_status": "awaiting_approval"}

    # Gate 3: CI must be green before auto-merge. A red CI triggers a recode; a
    # hang holds the merge. Repos with no CI workflow report no status and pass.
    ci_outcome = _ci_gate(r, owner, repo, pr_number, repo_full_name)
    if ci_outcome is not None:
        return ci_outcome

    # Gate 4: the PR must be conflict-free with its base. A story branched from a
    # stale main conflicts; try updating the branch from base, else recode to rebase.
    merge_outcome = _mergeability_gate(r, owner, repo, pr_number, repo_full_name)
    if merge_outcome is not None:
        return merge_outcome

    # All checks pass — auto-merge
    return _auto_merge(owner, repo, pr_number, repo_full_name)


def _wait_for_ci(
    fj: "ForgejoClient",
    owner: str,
    repo: str,
    pr_number: int,
    *,
    timeout: int,
    interval: int,
    grace: int,
    sleep=time.sleep,
    clock=time.monotonic,
) -> str:
    """
    Poll the PR head commit's combined CI status until it resolves.

    Returns:
      "success" — combined status is success
      "failure" — combined status is failure/error
      "none"    — no status reported within `grace` seconds (repo has no CI)
      "timeout" — status stayed pending past `timeout` seconds
    """
    start = clock()
    while True:
        try:
            pr = fj.get_pr(owner, repo, pr_number)
            sha = (pr.get("head") or {}).get("sha", "")
            status = fj.get_combined_status(owner, repo, sha) if sha else {}
        except Exception as exc:
            log.warning("ci_status_fetch_failed", repo=f"{owner}/{repo}", pr=pr_number, error=str(exc))
            status = {}
        statuses = status.get("statuses") or []
        state = status.get("state") or ""
        elapsed = clock() - start
        if statuses:
            if state == "success":
                return "success"
            if state in ("failure", "error"):
                return "failure"
        elif elapsed >= grace:
            return "none"
        if elapsed >= timeout:
            return "timeout"
        sleep(interval)


def _ci_gate(
    r: "redis_module.Redis",
    owner: str,
    repo: str,
    pr_number: int,
    repo_full_name: str,
) -> dict | None:
    """Wait for CI; return a gate-outcome dict if it blocks the merge, else None."""
    if not settings.ci_wait_enabled:
        return None

    with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as fj:
        result = _wait_for_ci(
            fj, owner, repo, pr_number,
            timeout=settings.ci_wait_timeout,
            interval=settings.ci_wait_interval,
            grace=settings.ci_wait_grace,
        )

    if result in ("success", "none"):
        log.info("ci_gate_pass", repo=repo_full_name, pr=pr_number, ci=result)
        return None

    if result == "failure":
        body = (
            "❌ **CI failed** — the automated test workflow did not pass.\n\n"
            "The coding agent will push a fix automatically."
        )
        with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as fj:
            fj.post_pr_comment(owner, repo, pr_number, body)
            pr_data = fj.get_pr(owner, repo, pr_number)
        head_ref = (pr_data.get("head") or {}).get("ref", "")
        pr_url = pr_data.get("html_url", "")
        _clear_verdicts(r, repo_full_name, pr_number)
        _trigger_recode(repo_full_name, pr_number, pr_url, head_ref, body)
        log.info("ci_gate_failed", repo=repo_full_name, pr=pr_number)
        return {"gate_status": "changes_requested", "failing": ["ci"]}

    # timeout — hold the merge for a human/retry rather than merging on unknown CI
    with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as fj:
        fj.post_pr_comment(
            owner, repo, pr_number,
            "⏳ **Merge held** — CI did not finish within the timeout. "
            "Push a new commit or re-run the workflow to retry.",
        )
    log.info("ci_gate_timeout", repo=repo_full_name, pr=pr_number)
    return {"gate_status": "blocked", "reason": "ci_timeout"}


def _mergeability_gate(
    r: "redis_module.Redis",
    owner: str,
    repo: str,
    pr_number: int,
    repo_full_name: str,
) -> dict | None:
    """Ensure the PR can merge cleanly. Returns a gate-outcome if it blocks, else None.

    If the PR isn't mergeable (its branch is behind/conflicts with base), first try
    updating the branch from base. If that resolves it, proceed; otherwise trigger a
    recode (the coder rebases on current base and resolves) instead of failing merge.
    """
    body = (
        "⚠️ **Merge conflict with base** — this branch is behind `main` and conflicts. "
        "The coding agent will rebase on the latest base and resolve."
    )
    try:
        with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as fj:
            pr = fj.get_pr(owner, repo, pr_number)
            if pr.get("mergeable") is not False:
                return None  # mergeable (or Forgejo hasn't computed it) — let merge proceed

            # Behind/conflicting — try to bring the branch up to date with base.
            if fj.update_pr_branch(owner, repo, pr_number):
                pr = fj.get_pr(owner, repo, pr_number)
                if pr.get("mergeable") is not False:
                    log.info("mergeability_resolved_by_update", repo=repo_full_name, pr=pr_number)
                    return None

            head_ref = (pr.get("head") or {}).get("ref", "")
            pr_url = pr.get("html_url", "")
            fj.post_pr_comment(owner, repo, pr_number, body)
    except Exception as exc:
        # Can't determine mergeability — don't block; _auto_merge surfaces real failures.
        log.warning("mergeability_gate_skipped", repo=repo_full_name, pr=pr_number, error=str(exc))
        return None

    _clear_verdicts(r, repo_full_name, pr_number)
    _trigger_recode(repo_full_name, pr_number, pr_url, head_ref, body)
    log.info("gate_merge_conflict", repo=repo_full_name, pr=pr_number)
    return {"gate_status": "changes_requested", "reason": "merge_conflict"}


def _auto_merge(owner: str, repo: str, pr_number: int, repo_full_name: str) -> dict:
    try:
        with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as fj:
            fj.merge_pr(owner, repo, pr_number)
            pr_data = fj.get_pr(owner, repo, pr_number)
        pr_url = pr_data.get("html_url", "")
        log.info("pr_auto_merged", repo=repo_full_name, pr=pr_number)
        # Notify event-bus directly — Forgejo doesn't reliably fire the merged webhook
        # for API-triggered merges, so we push the state update ourselves.
        _notify_merged(repo_full_name, pr_number, pr_url)
        return {"gate_status": "merged"}
    except Exception as exc:
        log.error("pr_auto_merge_failed", repo=repo_full_name, pr=pr_number, error=str(exc))
        return {"gate_status": "merge_error", "error": str(exc)}
