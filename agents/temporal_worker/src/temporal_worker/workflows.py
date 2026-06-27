"""
Temporal PR Review Workflow — Phase 6.

Replaces the fire-and-forget RQ fan-out with a durable Temporal workflow that:
  1. Fans out all 3 review activities concurrently (with independent retries)
  2. Posts the aggregated verdict comment
  3. Applies gate logic:
       - security_signoff ON + security failed  → blocks merge
       - pr_merge_approval ON                   → waits for 'approve' signal (human or API)
       - neither gate triggered                 → auto-merges immediately
  4. Merges the PR once approved / unblocked

Workflow ID convention:  pr-review-{owner}-{repo}-{pr_number}

To send an approval signal from the CLI:
  temporal workflow signal \\
    --workflow-id pr-review-myorg-myrepo-42 \\
    --name approve \\
    --input '"alice"'

Or via the event-bus:
  POST /api/prs/myorg/myrepo/42/approve  {"approver": "alice"}
"""

from __future__ import annotations
import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy


# ── Input / output types ──────────────────────────────────────────────────────

@dataclass
class PRReviewInput:
    repo_full_name: str
    pr_number: int
    head_sha: str
    base_ref: str = "main"
    model_reviewer: str = ""
    model_tester: str = ""
    model_security: str = ""


# ── Workflow ──────────────────────────────────────────────────────────────────

@workflow.defn
class PRReviewWorkflow:
    """
    Durable PR review workflow with optional human approval gate.
    Survives worker restarts — state is persisted in Temporal.
    """

    def __init__(self) -> None:
        self._approved = False
        self._approver = ""

    @workflow.run
    async def run(self, params: PRReviewInput) -> dict:
        retry = RetryPolicy(
            maximum_attempts=3,
            backoff_coefficient=2.0,
            initial_interval=timedelta(seconds=5),
        )

        # Fan out all 3 reviews concurrently; each has independent retries
        code_result, test_result, security_result = await asyncio.gather(
            workflow.execute_activity(
                run_code_review_activity,
                args=[params.repo_full_name, params.pr_number, params.head_sha,
                      params.base_ref, params.model_reviewer],
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=retry,
            ),
            workflow.execute_activity(
                run_test_activity,
                args=[params.repo_full_name, params.pr_number, params.head_sha,
                      params.base_ref, params.model_tester],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=retry,
            ),
            workflow.execute_activity(
                run_security_activity,
                args=[params.repo_full_name, params.pr_number, params.head_sha,
                      params.base_ref, params.model_security],
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=retry,
            ),
        )

        all_verdicts = {
            "code_review": code_result,
            "test_run": test_result,
            "security": security_result,
        }

        # Post aggregated summary and apply gate logic
        gate_result = await workflow.execute_activity(
            post_and_gate_activity,
            args=[params.repo_full_name, params.pr_number, all_verdicts],
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=retry,
        )

        gate_status = gate_result.get("gate_status", "unknown")

        if gate_status == "awaiting_approval":
            # Suspend until a human sends the 'approve' signal (timeout: 24 h)
            await workflow.wait_condition(
                lambda: self._approved,
                timeout=timedelta(hours=24),
            )
            gate_result = await workflow.execute_activity(
                merge_pr_activity,
                args=[params.repo_full_name, params.pr_number],
                start_to_close_timeout=timedelta(minutes=2),
            )
            gate_result["approver"] = self._approver

        return {
            "repo_full_name": params.repo_full_name,
            "pr_number": params.pr_number,
            "verdicts": all_verdicts,
            "gate": gate_result,
        }

    @workflow.signal
    async def approve(self, approver: str = "") -> None:
        """Signal sent by a human (via event-bus API or Temporal CLI) to approve merge."""
        self._approved = True
        self._approver = approver or "human"


# ── Activities ────────────────────────────────────────────────────────────────

@activity.defn
async def run_code_review_activity(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    base_ref: str,
    model_override: str,
) -> dict:
    from reviewer.code_review import run_code_review
    return run_code_review(repo_full_name, pr_number, head_sha, base_ref,
                           model_override=model_override)


@activity.defn
async def run_test_activity(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    base_ref: str,
    model_override: str,
) -> dict:
    from reviewer.test_runner import run_tests
    return run_tests(repo_full_name, pr_number, head_sha, base_ref,
                     model_override=model_override)


@activity.defn
async def run_security_activity(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    base_ref: str,
    model_override: str,
) -> dict:
    from reviewer.security_scan import run_security_scan
    return run_security_scan(repo_full_name, pr_number, head_sha, base_ref,
                             model_override=model_override)


@activity.defn
async def post_and_gate_activity(
    repo_full_name: str,
    pr_number: int,
    all_verdicts: dict,
) -> dict:
    """Post the aggregated summary and apply gate logic. Does NOT merge."""
    import redis
    from reviewer.config import settings
    from reviewer.verdicts import format_summary_comment, post_aggregated_and_gate
    from reviewer.forgejo_client import ForgejoClient

    owner, repo_name = repo_full_name.split("/", 1)
    r = redis.from_url(settings.redis_url, decode_responses=False)
    return post_aggregated_and_gate(r, repo_full_name, pr_number, all_verdicts)


@activity.defn
async def merge_pr_activity(repo_full_name: str, pr_number: int) -> dict:
    """Merge the PR after human approval."""
    owner, repo_name = repo_full_name.split("/", 1)
    from reviewer.forgejo_client import ForgejoClient
    from reviewer.config import settings
    with ForgejoClient(settings.forgejo_base_url, settings.forgejo_api_token) as fj:
        return fj.merge_pr(owner, repo_name, pr_number)
