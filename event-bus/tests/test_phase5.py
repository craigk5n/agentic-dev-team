"""Tests for Phase 5: Planner Agent wiring, /api/ideas endpoint."""

from unittest.mock import patch


# ── /api/ideas endpoint ───────────────────────────────────────────────────────

class TestIdeasEndpoint:
    def test_submit_idea_success(self, client, monkeypatch):
        fake_item = {"id": "i-1", "title": "Auth System", "state": "pending-approval", "type": "idea"}
        with patch("idea_agent.main.expand_idea", return_value={"title": "Auth System", "description": "desc"}), \
             patch("event_bus.main.create_item", return_value=fake_item):
            resp = client.post("/api/ideas", json={"prompt": "Add JWT auth"})
        assert resp.status_code == 202
        assert resp.json()["title"] == "Auth System"
        assert resp.json()["state"] == "pending-approval"

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

    def test_model_override_in_body(self, client, monkeypatch):
        fake_item = {"id": "i-2", "title": "T", "state": "pending-approval", "type": "idea"}
        with patch("idea_agent.main.expand_idea", return_value={"title": "T", "description": "D"}) as mock, \
             patch("event_bus.main.create_item", return_value=fake_item):
            resp = client.post("/api/ideas", json={"prompt": "idea", "model_override": "gpt-4"})
        assert resp.status_code == 202
        assert mock.call_args.kwargs["model_override"] == "gpt-4"

    def test_model_override_from_settings(self, client, monkeypatch):
        fake_item = {"id": "i-3", "title": "T", "state": "pending-approval", "type": "idea"}
        with patch("idea_agent.main.expand_idea", return_value={"title": "T", "description": "D"}), \
             patch("event_bus.main.create_item", return_value=fake_item):
            resp = client.post("/api/ideas", json={"prompt": "idea"})
        assert resp.status_code == 202

    def test_agent_exception_returns_500(self, client, monkeypatch):
        with patch("idea_agent.main.expand_idea", side_effect=RuntimeError("LLM error")):
            resp = client.post("/api/ideas", json={"prompt": "test"})
        assert resp.status_code == 500
