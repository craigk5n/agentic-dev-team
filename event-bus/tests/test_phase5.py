"""Tests for Phase 5: Idea handler, Planner Agent wiring, /api/ideas endpoint."""

import json
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import sign_plane


# ── handle_idea_approved ──────────────────────────────────────────────────────

class TestHandleIdeaApproved:
    def test_delegates_to_planner(self):
        import fakeredis
        from event_bus.jobs.handlers import handle_idea_approved
        r = fakeredis.FakeRedis()
        with patch("planner_agent.main.run_planner", return_value={"status": "planned"}) as mock, \
             patch("redis.from_url", return_value=r):
            result = handle_idea_approved("idea-1", "ws", "proj-1")
        mock.assert_called_once_with(issue_id="idea-1", workspace_slug="ws", project_id="proj-1", model_override="")
        assert result["status"] == "planned"

    def test_missing_package_returns_error(self):
        from event_bus.jobs.handlers import handle_idea_approved
        with patch.dict("sys.modules", {"planner_agent": None, "planner_agent.main": None}):
            result = handle_idea_approved("idea-1", "ws", "proj-1")
        assert result["status"] == "error"
        assert "planner_agent" in result["reason"]


# ── /api/ideas endpoint ───────────────────────────────────────────────────────

class TestIdeasEndpoint:
    def test_submit_idea_success(self, client, monkeypatch):
        idea_result = {"status": "pending_approval", "issue_id": "i-1", "title": "Auth System", "url": "http://plane/..."}
        monkeypatch.setattr("event_bus.main.settings.plane_project_id", "proj-1")
        with patch("idea_agent.main.submit_idea", return_value=idea_result):
            resp = client.post("/api/ideas", json={"prompt": "Add JWT auth"})
        assert resp.status_code == 202
        assert resp.json()["issue_id"] == "i-1"

    def test_missing_prompt_returns_400(self, client):
        resp = client.post("/api/ideas", json={"other": "field"})
        assert resp.status_code == 400
        assert "prompt" in resp.json()["detail"]

    def test_empty_prompt_returns_400(self, client):
        resp = client.post("/api/ideas", json={"prompt": "   "})
        assert resp.status_code == 400

    def test_invalid_json_returns_400(self, client):
        resp = client.post("/api/ideas", content=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status_code == 400

    def test_project_id_from_request_body(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main.settings.plane_project_id", "")
        idea_result = {"status": "pending_approval", "issue_id": "i-2", "title": "T", "url": "u"}
        with patch("idea_agent.main.submit_idea", return_value=idea_result) as mock:
            resp = client.post("/api/ideas", json={"prompt": "idea", "project_id": "proj-99"})
        assert resp.status_code == 202
        assert mock.call_args[1]["project_id"] == "proj-99"

    def test_missing_project_id_returns_422(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main.settings.plane_project_id", "")
        resp = client.post("/api/ideas", json={"prompt": "build something"})
        assert resp.status_code == 422

    def test_agent_exception_returns_500(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main.settings.plane_project_id", "proj-1")
        with patch("idea_agent.main.submit_idea", side_effect=RuntimeError("Plane error")):
            resp = client.post("/api/ideas", json={"prompt": "test"})
        assert resp.status_code == 500


# ── end-to-end: Plane webhook triggers handle_idea_approved ──────────────────

class TestWebhookToPlanner:
    def test_approved_idea_webhook_enqueues_planner(self, client, mock_queue, monkeypatch):
        from tests.conftest import PLANE_IDEA_APPROVED
        payload = json.dumps(PLANE_IDEA_APPROVED).encode()
        sig = sign_plane(payload)
        resp = client.post(
            "/webhook/plane",
            content=payload,
            headers={"Content-Type": "application/json", "X-Plane-Signature": sig},
        )
        assert resp.status_code == 202
        assert mock_queue.enqueue.called
        # The enqueued function should be handle_idea_approved
        from event_bus.jobs.handlers import handle_idea_approved
        enqueue_call = mock_queue.enqueue.call_args
        assert enqueue_call[0][0] is handle_idea_approved
        assert enqueue_call[1]["issue_id"] == "idea-xyz"
