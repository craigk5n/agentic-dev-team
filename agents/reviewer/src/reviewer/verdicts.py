"""
Redis-backed verdict store for PR review aggregation.

Each of the 3 review jobs (code_review, test_run, security) stores its verdict
independently. The last one to arrive finds all 3 present, wins the aggregation
race via SETNX, and triggers the summary comment.
"""

from __future__ import annotations
import json
from typing import TYPE_CHECKING, cast

import redis as redis_module
import structlog

if TYPE_CHECKING:
    pass  # avoid circular imports

log = structlog.get_logger()

ROLES = ("code_review", "test_run", "security")


def _pr_key(repo_full_name: str, pr_number: int) -> str:
    owner, repo = repo_full_name.split("/", 1)
    return f"{owner}:{repo}:{pr_number}"


def _verdict_key(pr_key: str, role: str) -> str:
    return f"pr_verdict:{pr_key}:{role}"


def _agg_lock_key(pr_key: str) -> str:
    return f"pr_verdict:{pr_key}:aggregated"


def _final_key(pr_key: str) -> str:
    return f"pr_verdict_final:{pr_key}"


# The per-role verdict keys are short-lived (TTL) and the gate deletes them on
# aggregation so the next push re-reviews. That leaves merged/done stories with no
# verdicts to show on the board. This durable snapshot preserves the aggregated
# statuses so the R/T/S pips survive past the review round. Kept 90 days.
_FINAL_TTL = 90 * 86_400


def store_final_verdicts(
    r: "redis_module.Redis",
    repo_full_name: str,
    pr_number: int,
    all_verdicts: dict,
) -> None:
    """Persist the aggregated per-role statuses durably (survives the gate's clear + TTL)
    so the board can show R/T/S on completed stories."""
    snapshot = {role: (all_verdicts.get(role, {}) or {}).get("status", "warn") for role in ROLES}
    r.setex(_final_key(_pr_key(repo_full_name, pr_number)), _FINAL_TTL, json.dumps(snapshot))


def store_verdict(
    r: "redis_module.Redis",
    repo_full_name: str,
    pr_number: int,
    role: str,
    verdict: dict,
    ttl: int = 3600,
) -> None:
    key = _verdict_key(_pr_key(repo_full_name, pr_number), role)
    r.setex(key, ttl, json.dumps(verdict))
    log.info("verdict_stored", repo=repo_full_name, pr=pr_number, role=role, status=verdict.get("status"))


def try_collect_all(
    r: "redis_module.Redis",
    repo_full_name: str,
    pr_number: int,
) -> dict | None:
    """Return all 3 verdicts if every role has reported, else None."""
    pkey = _pr_key(repo_full_name, pr_number)
    collected: dict[str, dict] = {}
    for role in ROLES:
        data = r.get(_verdict_key(pkey, role))
        if data is None:
            return None
        collected[role] = json.loads(cast(bytes, data))
    return collected


def try_claim_aggregation(
    r: "redis_module.Redis",
    repo_full_name: str,
    pr_number: int,
    ttl: int = 3600,
) -> bool:
    """Returns True if this caller wins the right to post the aggregated summary."""
    pkey = _pr_key(repo_full_name, pr_number)
    lock_key = _agg_lock_key(pkey)
    claimed = bool(r.setnx(lock_key, "1"))
    if claimed:
        r.expire(lock_key, ttl)
    return claimed


def store_and_check(
    r: "redis_module.Redis",
    repo_full_name: str,
    pr_number: int,
    role: str,
    verdict: dict,
    ttl: int = 3600,
) -> dict | None:
    """
    Store a verdict then check if aggregation is ready.
    Returns all verdicts if all 3 are in AND this caller won the agg race.
    Returns None otherwise (aggregation already claimed, or not all in yet).
    """
    store_verdict(r, repo_full_name, pr_number, role, verdict, ttl)
    all_in = try_collect_all(r, repo_full_name, pr_number)
    if all_in is None:
        return None
    if not try_claim_aggregation(r, repo_full_name, pr_number, ttl):
        return None
    return all_in


def aggregate_status(all_verdicts: dict) -> str:
    """Compute overall PR status from all role verdicts."""
    statuses = {v.get("status", "warn") for v in all_verdicts.values()}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "pass"


def format_summary_comment(all_verdicts: dict) -> str:
    """Render the aggregated PR review summary as markdown."""
    icons = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    overall = aggregate_status(all_verdicts)
    overall_icon = icons.get(overall, "❓")

    rows = []
    for role in ROLES:
        v = all_verdicts.get(role, {})
        status = v.get("status", "warn")
        summary = v.get("summary", "—")
        label = {"code_review": "Code Review", "test_run": "Tests", "security": "Security"}[role]
        rows.append(f"| {label} | {icons.get(status, '❓')} {status.capitalize()} | {summary} |")

    table = "\n".join([
        "| Check | Status | Summary |",
        "|-------|--------|---------|",
        *rows,
    ])

    conclusions = {
        "pass": "All checks passed — ready to merge.",
        "warn": "Warnings present — review before merging.",
        "fail": "One or more checks failed — changes required.",
    }

    return (
        f"## {overall_icon} Review Summary\n\n"
        f"{table}\n\n"
        f"**Overall:** {overall_icon} {conclusions.get(overall, '')}"
    )


def post_aggregated_and_gate(
    r: "redis_module.Redis",
    repo_full_name: str,
    pr_number: int,
    all_verdicts: dict,
) -> dict:
    """
    Post the aggregated review summary comment then apply merge gate logic.
    Called by whichever of the 3 review jobs wins the aggregation race.
    Returns the gate outcome dict.
    """
    from reviewer.config import settings
    from reviewer.forgejo_client import ForgejoClient
    from reviewer.gate import apply_gate

    owner, repo = repo_full_name.split("/", 1)
    body = format_summary_comment(all_verdicts)
    with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as fj:
        fj.post_pr_comment(owner, repo, pr_number, body)
    log.info("aggregated_verdict_posted", repo=repo_full_name, pr=pr_number)

    # Snapshot the aggregated statuses durably BEFORE apply_gate (which deletes the
    # per-role keys) so the board can show R/T/S on completed stories.
    try:
        store_final_verdicts(r, repo_full_name, pr_number, all_verdicts)
    except Exception as exc:
        log.warning("final_verdict_snapshot_failed", repo=repo_full_name, pr=pr_number, error=str(exc)[:120])

    return apply_gate(r, repo_full_name, pr_number, all_verdicts)
