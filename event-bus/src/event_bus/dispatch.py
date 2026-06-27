"""
Dispatch parsed events to the appropriate RQ job.

Plane issue events → check status transition → enqueue handler
Forgejo PR events  → check action           → enqueue handler
"""

from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
import structlog

from rq import Queue

from event_bus.events.forgejo import ForgejoPREvent
from event_bus.events.plane import PlaneEvent
from event_bus.jobs.handlers import handle_idea_approved, handle_story_ready, handle_pr_event
from event_bus.status import IdeaStatus, StoryStatus, state_name_to_status

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


def dispatch_plane_event(
    event: PlaneEvent,
    queue: Queue,
    workspace_slug: str,
) -> DispatchOutcome:
    """
    Route a Plane issue event to the right job handler.
    Returns SKIPPED for events that don't match a trigger state transition.
    """
    if not event.is_issue_event():
        return DispatchOutcome(DispatchResult.SKIPPED, f"non-issue event: {event.event}")

    if event.action not in ("created", "updated"):
        return DispatchOutcome(DispatchResult.SKIPPED, f"action not watched: {event.action}")

    state_name = event.state_name
    if not state_name:
        return DispatchOutcome(DispatchResult.SKIPPED, "no state_detail in payload")

    status = state_name_to_status(state_name)
    if status is None:
        return DispatchOutcome(DispatchResult.SKIPPED, f"unmapped state: {state_name!r}")

    issue_id = event.issue_id
    project_id = event.project_id

    if status == IdeaStatus.APPROVED:
        job = queue.enqueue(
            handle_idea_approved,
            issue_id=issue_id,
            workspace_slug=workspace_slug,
            project_id=project_id,
        )
        log.info("enqueued", handler="idea_approved", issue=issue_id, job=job.id)
        return DispatchOutcome(DispatchResult.ENQUEUED, "idea_approved", job_id=job.id)

    if status == StoryStatus.READY:
        job = queue.enqueue(
            handle_story_ready,
            issue_id=issue_id,
            workspace_slug=workspace_slug,
            project_id=project_id,
        )
        log.info("enqueued", handler="story_ready", issue=issue_id, job=job.id)
        return DispatchOutcome(DispatchResult.ENQUEUED, "story_ready", job_id=job.id)

    return DispatchOutcome(DispatchResult.SKIPPED, f"no trigger for status: {status}")


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
