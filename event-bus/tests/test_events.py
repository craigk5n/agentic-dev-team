from event_bus.events.forgejo import ForgejoPREvent
from tests.conftest import FORGEJO_PR_OPENED


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
