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
from typing import TYPE_CHECKING

import structlog

from reviewer.config import settings
from reviewer.forgejo_client import ForgejoClient

if TYPE_CHECKING:
    import redis as redis_module

log = structlog.get_logger()

_CONFIG_KEY = "runtime_config"
_PENDING_PREFIX = "pr_merge_pending:"


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
    from reviewer.verdicts import aggregate_status

    owner, repo = repo_full_name.split("/", 1)
    gates = _read_gates(r)

    overall = aggregate_status(all_verdicts)
    security_status = all_verdicts.get("security", {}).get("status", "warn")

    # Gate 0: any check failed — post a formal REQUEST_CHANGES review so Forgejo
    # fires pull_request_review_rejected, which triggers the recode agent.
    # Clear the stale verdicts so the next push starts a fresh review round.
    if overall == "fail":
        failing = [
            role for role, v in all_verdicts.items() if v.get("status") == "fail"
        ]
        body = (
            "❌ **Changes required** — the following checks failed: "
            + ", ".join(failing)
            + ".\n\nThe coding agent will push a fix automatically."
        )
        with ForgejoClient(settings.forgejo_base_url, settings.forgejo_api_token) as fj:
            fj.create_review(owner, repo, pr_number, "REQUEST_CHANGES", body)
        _clear_verdicts(r, repo_full_name, pr_number)
        log.info("gate_changes_requested", repo=repo_full_name, pr=pr_number, failing=failing)
        return {"gate_status": "changes_requested", "failing": failing}

    # Gate 1: security signoff — block merge on security warnings too if flag set
    if gates.get("security_signoff", True) and security_status == "fail":
        msg = (
            "⛔ **Merge blocked** — security scan failed.\n\n"
            "Resolve the findings above and push a new commit to re-run checks."
        )
        with ForgejoClient(settings.forgejo_base_url, settings.forgejo_api_token) as fj:
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
        with ForgejoClient(settings.forgejo_base_url, settings.forgejo_api_token) as fj:
            fj.post_pr_comment(owner, repo, pr_number, msg)
        log.info("gate_awaiting_approval", repo=repo_full_name, pr=pr_number)
        return {"gate_status": "awaiting_approval"}

    # All checks pass — auto-merge
    return _auto_merge(owner, repo, pr_number, repo_full_name)


def _auto_merge(owner: str, repo: str, pr_number: int, repo_full_name: str) -> dict:
    try:
        with ForgejoClient(settings.forgejo_base_url, settings.forgejo_api_token) as fj:
            fj.merge_pr(owner, repo, pr_number)
        log.info("pr_auto_merged", repo=repo_full_name, pr=pr_number)
        return {"gate_status": "merged"}
    except Exception as exc:
        log.error("pr_auto_merge_failed", repo=repo_full_name, pr=pr_number, error=str(exc))
        return {"gate_status": "merge_error", "error": str(exc)}
