from enum import Enum


class IdeaStatus(str, Enum):
    PENDING_APPROVAL = "pending-approval"
    APPROVED = "approved"
    REJECTED = "rejected"


class StoryStatus(str, Enum):
    BACKLOG = "backlog"
    READY = "ready"
    IN_PROGRESS = "in-progress"
    IN_REVIEW = "in-review"
    CHANGES_REQUESTED = "changes-requested"
    APPROVED = "approved"
    MERGED = "merged"
    DONE = "done"


# Plane state names that map to our status model.
# Keys are lowercased for case-insensitive lookup.
_STATE_MAP: dict[str, IdeaStatus | StoryStatus] = {
    "pending-approval": IdeaStatus.PENDING_APPROVAL,
    "pending approval": IdeaStatus.PENDING_APPROVAL,
    "approved": IdeaStatus.APPROVED,
    "rejected": IdeaStatus.REJECTED,
    "backlog": StoryStatus.BACKLOG,
    "ready": StoryStatus.READY,
    "in-progress": StoryStatus.IN_PROGRESS,
    "in progress": StoryStatus.IN_PROGRESS,
    "in-review": StoryStatus.IN_REVIEW,
    "in review": StoryStatus.IN_REVIEW,
    "changes-requested": StoryStatus.CHANGES_REQUESTED,
    "changes requested": StoryStatus.CHANGES_REQUESTED,
    "merged": StoryStatus.MERGED,
    "done": StoryStatus.DONE,
}

# Status transitions that trigger agent job dispatch
AGENT_TRIGGERS: dict[IdeaStatus | StoryStatus, str] = {
    IdeaStatus.APPROVED: "plan",   # → Planner Agent (Phase 5)
    StoryStatus.READY: "code",     # → Coding Agent (Phase 3)
}


def state_name_to_status(name: str) -> IdeaStatus | StoryStatus | None:
    return _STATE_MAP.get(name.lower().strip())
