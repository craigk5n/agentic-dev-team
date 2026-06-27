"""Tests for Phase 6: runtime config API, PR approval endpoint, model overrides."""

from __future__ import annotations
import json
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import fakeredis
import pytest


# ── Config store unit tests ───────────────────────────────────────────────────

class TestConfigStore:
    def test_get_config_returns_defaults_when_empty(self):
        r = fakeredis.FakeRedis()
        from event_bus.config_store import get_config
        config = get_config(r)
        assert config.gates.idea_approval is True
        assert config.gates.pr_merge_approval is False
        assert config.gates.security_signoff is True
        assert config.models.reviewer == ""

    def test_patch_config_updates_gate(self):
        r = fakeredis.FakeRedis()
        from event_bus.config_store import patch_config
        config = patch_config(r, {"gates": {"pr_merge_approval": True}})
        assert config.gates.pr_merge_approval is True
        assert config.gates.security_signoff is True  # untouched

    def test_patch_config_updates_model(self):
        r = fakeredis.FakeRedis()
        from event_bus.config_store import patch_config
        config = patch_config(r, {"models": {"reviewer": "openrouter/openai/gpt-4o"}})
        assert config.models.reviewer == "openrouter/openai/gpt-4o"
        assert config.models.tester == ""  # untouched

    def test_patch_config_idea_approval_is_immutable(self):
        r = fakeredis.FakeRedis()
        from event_bus.config_store import patch_config
        config = patch_config(r, {"gates": {"idea_approval": False}})
        # idea_approval must stay True — it's read-only
        assert config.gates.idea_approval is True

    def test_patch_config_ignores_unknown_roles(self):
        r = fakeredis.FakeRedis()
        from event_bus.config_store import patch_config
        config = patch_config(r, {"models": {"unknown_role": "gpt-99"}})
        # should not raise; unknown roles are silently skipped
        assert not hasattr(config.models, "unknown_role")

    def test_patch_config_persists_across_reads(self):
        r = fakeredis.FakeRedis()
        from event_bus.config_store import get_config, patch_config
        patch_config(r, {"gates": {"security_signoff": False}, "models": {"coder": "ollama/mistral"}})
        config = get_config(r)
        assert config.gates.security_signoff is False
        assert config.models.coder == "ollama/mistral"

    def test_patch_config_multiple_calls_accumulate(self):
        r = fakeredis.FakeRedis()
        from event_bus.config_store import patch_config
        patch_config(r, {"models": {"reviewer": "gpt-4o"}})
        patch_config(r, {"models": {"tester": "claude-haiku"}})
        from event_bus.config_store import get_config
        config = get_config(r)
        assert config.models.reviewer == "gpt-4o"
        assert config.models.tester == "claude-haiku"


# ── GET /api/config endpoint ──────────────────────────────────────────────────

class TestGetConfig:
    def test_returns_default_config(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.get("/api/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["gates"]["idea_approval"] is True
        assert body["gates"]["pr_merge_approval"] is False
        assert body["gates"]["security_signoff"] is True
        assert body["models"]["reviewer"] == ""

    def test_returns_updated_config_after_patch(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        client.patch("/api/config", json={"models": {"reviewer": "gpt-4o"}})
        resp = client.get("/api/config")
        assert resp.json()["models"]["reviewer"] == "gpt-4o"


# ── PATCH /api/config endpoint ────────────────────────────────────────────────

class TestPatchConfig:
    def test_patch_gate_flag(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.patch("/api/config", json={"gates": {"pr_merge_approval": True}})
        assert resp.status_code == 200
        assert resp.json()["gates"]["pr_merge_approval"] is True

    def test_patch_model_override(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.patch("/api/config", json={"models": {"reviewer": "openrouter/openai/gpt-4o"}})
        assert resp.status_code == 200
        assert resp.json()["models"]["reviewer"] == "openrouter/openai/gpt-4o"

    def test_patch_invalid_json_returns_400(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.patch("/api/config", content=b"not json",
                            headers={"Content-Type": "application/json"})
        assert resp.status_code == 400

    def test_patch_all_roles(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        overrides = {
            "idea": "a", "planner": "b", "coder": "c",
            "reviewer": "d", "tester": "e", "security": "f",
        }
        resp = client.patch("/api/config", json={"models": overrides})
        assert resp.status_code == 200
        models = resp.json()["models"]
        for role, val in overrides.items():
            assert models[role] == val


# ── POST /api/prs/.../approve endpoint (RQ fallback path) ────────────────────

class TestPRApproveEndpoint:
    def test_approve_enqueues_merge_when_pending(self, client, mock_queue, monkeypatch):
        r = fakeredis.FakeRedis()
        r.setex("pr_merge_pending:myorg:myrepo:7", 86400, json.dumps({"pr_number": 7}))
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        monkeypatch.setattr("event_bus.main.settings.temporal_address", "")
        monkeypatch.setattr("event_bus.main._queue", mock_queue)
        resp = client.post("/api/prs/myorg/myrepo/7/approve", json={"approver": "alice"})
        assert resp.status_code == 202
        assert resp.json()["status"] == "merging"
        mock_queue.enqueue.assert_called_once()
        # Pending key should be deleted
        assert not r.exists("pr_merge_pending:myorg:myrepo:7")

    def test_approve_returns_404_when_no_pending(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        monkeypatch.setattr("event_bus.main.settings.temporal_address", "")
        resp = client.post("/api/prs/myorg/myrepo/99/approve")
        assert resp.status_code == 404

    def test_approve_with_temporal_returns_error_when_unreachable(self, client, monkeypatch):
        """When Temporal is configured but unreachable, return 502."""
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        monkeypatch.setattr("event_bus.main.settings.temporal_address", "temporal:7233")
        pytest.importorskip("temporalio", reason="temporalio not installed")
        resp = client.post("/api/prs/myorg/myrepo/3/approve", json={"approver": "bob"})
        # Without Temporal running, the signal will fail → 502
        assert resp.status_code == 502


# ── Model override flows through job enqueue ──────────────────────────────────

class TestModelOverrideInPREvent:
    def test_model_override_passed_to_jobs(self, monkeypatch):
        r = fakeredis.FakeRedis()
        import json as _json
        r.set("runtime_config", _json.dumps({"gates": {}, "models": {"reviewer": "gpt-4o", "tester": "haiku", "security": ""}}))

        mock_queue = MagicMock()
        job = MagicMock()
        job.id = "job-1"
        mock_queue.enqueue.return_value = job

        with patch("redis.from_url", return_value=r), \
             patch("rq.Queue", return_value=mock_queue):
            from event_bus.jobs.handlers import handle_pr_event
            handle_pr_event("org/repo", 5, "abc123", "opened")

        calls = mock_queue.enqueue.call_args_list
        assert len(calls) == 3

        reviewer_call = calls[0]
        assert reviewer_call[1].get("model_override") == "gpt-4o"

        tester_call = calls[1]
        assert tester_call[1].get("model_override") == "haiku"

        security_call = calls[2]
        assert security_call[1].get("model_override") == ""


# ── /api/ideas model_override support ────────────────────────────────────────

class TestIdeasModelOverride:
    def test_model_override_from_request_body(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        monkeypatch.setattr("event_bus.main.settings.plane_project_id", "proj-1")
        idea_result = {"status": "pending_approval", "issue_id": "i-1", "title": "T", "url": "u"}
        with patch("idea_agent.main.submit_idea", return_value=idea_result) as mock:
            client.post("/api/ideas", json={"prompt": "test", "model_override": "ollama/mistral"})
        assert mock.call_args[1]["model_override"] == "ollama/mistral"

    def test_model_override_from_runtime_config(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        import json as _json
        r.set("runtime_config", _json.dumps({"models": {"idea": "gpt-4o"}}))
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        monkeypatch.setattr("event_bus.main.settings.plane_project_id", "proj-1")
        idea_result = {"status": "pending_approval", "issue_id": "i-1", "title": "T", "url": "u"}
        with patch("idea_agent.main.submit_idea", return_value=idea_result) as mock:
            client.post("/api/ideas", json={"prompt": "test"})
        assert mock.call_args[1]["model_override"] == "gpt-4o"

    def test_request_body_override_wins_over_config(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        import json as _json
        r.set("runtime_config", _json.dumps({"models": {"idea": "config-model"}}))
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        monkeypatch.setattr("event_bus.main.settings.plane_project_id", "proj-1")
        idea_result = {"status": "pending_approval", "issue_id": "i-1", "title": "T", "url": "u"}
        with patch("idea_agent.main.submit_idea", return_value=idea_result) as mock:
            client.post("/api/ideas", json={"prompt": "test", "model_override": "request-model"})
        assert mock.call_args[1]["model_override"] == "request-model"
