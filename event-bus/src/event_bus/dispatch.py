"""
Dispatch parsed events to the appropriate RQ job.

Forgejo PR events → check action → enqueue handler
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import structlog

from rq import Queue

from event_bus.events.forgejo import ForgejoPREvent
from event_bus.jobs.handlers import handle_pr_event

log = structlog.get_logger()


class DispatchResult(str, Enum):
    ENQUEUED = "enqueued"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class DispatchOutcome:
    result: DispatchResult
    reason: str
    job_id: str | None = None


def dispatch_forgejo_event(
    event: ForgejoPREvent,
    queue: Queue,
) -> DispatchOutcome:
    """Route a Forgejo PR event to review fan-out."""
    if not event.is_review_trigger():
        return DispatchOutcome(DispatchResult.SKIPPED, f"PR action not watched: {event.action}")

    job = queue.enqueue(
        handle_pr_event,
        repo_full_name=event.repo_full_name,
        pr_number=event.pr_number,
        head_sha=event.head_sha,
        head_ref=event.head_ref,
        base_ref=event.base_ref,
        action=event.action,
    )
    log.info("enqueued", handler="pr_event", repo=event.repo_full_name, pr=event.pr_number, job=job.id)
    return DispatchOutcome(DispatchResult.ENQUEUED, "pr_event", job_id=job.id)
