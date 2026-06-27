"""
Temporal worker entry point — runs the PR review task queue.

Start via:
  python -m temporal_worker.worker

Or as a Docker service (see infra/temporal/docker-compose.yml).

Required environment variables:
  TEMPORAL_ADDRESS   — e.g. localhost:7233 or temporal:7233 (Docker)
  FORGEJO_API_TOKEN
  FORGEJO_BASE_URL
  REDIS_URL
  ANTHROPIC_API_KEY or OPENROUTER_API_KEY (for LLM calls in activities)
"""

from __future__ import annotations
import asyncio
import logging
import os

log = logging.getLogger(__name__)

TASK_QUEUE = "pr-review"


async def main() -> None:
    try:
        from temporalio.client import Client
        from temporalio.worker import Worker
    except ImportError:
        raise SystemExit(
            "temporalio package not installed. "
            "Install with: pip install 'temporalio>=1.4'"
        )

    from temporal_worker.workflows import (
        PRReviewWorkflow,
        merge_pr_activity,
        post_and_gate_activity,
        run_code_review_activity,
        run_security_activity,
        run_test_activity,
    )

    address = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")
    client = await Client.connect(address)

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[PRReviewWorkflow],
        activities=[
            run_code_review_activity,
            run_test_activity,
            run_security_activity,
            post_and_gate_activity,
            merge_pr_activity,
        ],
    )

    log.info("temporal_worker_started", extra={"task_queue": TASK_QUEUE, "address": address})
    print(f"Temporal worker started | task_queue={TASK_QUEUE} | address={address}")
    await worker.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
