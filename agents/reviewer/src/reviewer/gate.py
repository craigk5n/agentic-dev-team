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
    Posts a follow-up PR comment and returns the gate outcome.
    """
    owner, repo = repo_full_name.split("/", 1)
    gates = _read_gates(r)

    security_status = all_verdicts.get("security", {}).get("status", "warn")

    # Gate 1: security signoff — block merge on security failures
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

    # No gate blocking — auto-merge
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
