import pytest
from event_bus.events.plane import PlaneEvent
from event_bus.events.forgejo import ForgejoPREvent
from event_bus.status import IdeaStatus, StoryStatus, state_name_to_status
from tests.conftest import (
    FORGEJO_PR_OPENED,
    PLANE_IDEA_APPROVED,
    PLANE_ISSUE_READY,
)


class TestPlaneEvent:
    def test_parses_payload_key(self):
        evt = PlaneEvent.model_validate(PLANE_ISSUE_READY)
        assert evt.event == "issue"
        assert evt.action == "updated"
        assert evt.issue_id == "issue-abc"
        assert evt.state_name == "Ready"

    def test_parses_data_key_as_fallback(self):
        raw = {**PLANE_ISSUE_READY, "data": PLANE_ISSUE_READY["payload"]}
        raw.pop("payload")
        evt = PlaneEvent.model_validate(raw)
        assert evt.state_name == "Ready"

    def test_idea_approved(self):
        evt = PlaneEvent.model_validate(PLANE_IDEA_APPROVED)
        assert evt.state_name == "Approved"
        assert evt.issue_id == "idea-xyz"

    def test_is_issue_event_true(self):
        evt = PlaneEvent.model_validate(PLANE_ISSUE_READY)
        assert evt.is_issue_event() is True

    def test_is_issue_event_false_for_cycle(self):
        raw = {**PLANE_ISSUE_READY, "event": "cycle"}
        evt = PlaneEvent.model_validate(raw)
        assert evt.is_issue_event() is False

    def test_no_state_detail(self):
        raw = {
            "event": "issue",
            "action": "updated",
            "payload": {"id": "abc", "name": "test"},
        }
        evt = PlaneEvent.model_validate(raw)
        assert evt.state_name is None
        assert evt.state_detail is None


class TestForgejoPREvent:
    def test_parses_opened_event(self):
        evt = ForgejoPREvent.model_validate(FORGEJO_PR_OPENED)
        assert evt.action == "opened"
        assert evt.pr_number == 42
        assert evt.repo_full_name == "dev/myrepo"
        assert evt.head_sha == "abc123def456"

    def test_is_review_trigger_for_opened(self):
        evt = ForgejoPREvent.model_validate(FORGEJO_PR_OPENED)
        assert evt.is_review_trigger() is True

    def test_is_review_trigger_for_synchronize(self):
        raw = {**FORGEJO_PR_OPENED, "action": "synchronize"}
        evt = ForgejoPREvent.model_validate(raw)
        assert evt.is_review_trigger() is True

    def test_not_review_trigger_for_closed(self):
        raw = {**FORGEJO_PR_OPENED, "action": "closed"}
        evt = ForgejoPREvent.model_validate(raw)
        assert evt.is_review_trigger() is False


class TestStatusMapping:
    @pytest.mark.parametrize("name,expected", [
        ("Ready", StoryStatus.READY),
        ("ready", StoryStatus.READY),
        ("In Progress", StoryStatus.IN_PROGRESS),
        ("in-progress", StoryStatus.IN_PROGRESS),
        ("Approved", IdeaStatus.APPROVED),
        ("pending-approval", IdeaStatus.PENDING_APPROVAL),
        ("Pending Approval", IdeaStatus.PENDING_APPROVAL),
        ("done", StoryStatus.DONE),
        ("merged", StoryStatus.MERGED),
    ])
    def test_known_states(self, name, expected):
        assert state_name_to_status(name) == expected

    def test_unknown_state_returns_none(self):
        assert state_name_to_status("Weird Custom State") is None

    def test_whitespace_trimmed(self):
        assert state_name_to_status("  Ready  ") == StoryStatus.READY
