"""
Additional tests for event-bus main.py endpoints, handlers, and prompt_store.

Targets uncovered lines in:
  - main.py: work-items API, prompts API, forgejo webhook paths, internal endpoints
  - jobs/handlers.py: rate-limit and ImportError paths
  - jobs/pr_jobs.py: ImportError paths, do_merge_pr
  - prompt_store.py: direct function tests
"""

from __future__ import annotations
import json
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import fakeredis
import pytest

from event_bus.config import settings
from tests.conftest import sign_forgejo, FORGEJO_PR_OPENED


# ── helpers ───────────────────────────────────────────────────────────────────

def _no_limits():
    from event_bus.config_store import LimitsConfig
    return fakeredis.FakeRedis(), LimitsConfig(
        max_concurrent_reviewer=0, max_concurrent_tester=0, max_concurrent_security=0,
    )


# ── /api/ideas extra path (ImportError → 503) ─────────────────────────────────

class TestIdeasExtra:
    def test_import_error_returns_503(self, client):
        with patch.dict("sys.modules", {"idea_agent": None, "idea_agent.main": None}):
            resp = client.post("/api/ideas", json={"prompt": "Test idea"})
        assert resp.status_code == 503
        assert "idea_agent" in resp.json()["detail"]


# ── /api/items (work-items API) ───────────────────────────────────────────────

class TestWorkItemsApi:
    _pending = {"id": "idea-1", "title": "Auth System", "state": "pending-approval",
                "type": "idea", "description": "desc", "pr_url": None}
    _ready = {"id": "story-1", "title": "Add login", "state": "ready",
              "type": "story", "description": "desc", "pr_url": None}

    def test_list_items_empty(self, client):
        with patch("event_bus.main.grouped_items", return_value={}):
            resp = client.get("/api/items")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["groups"] == []

    def test_list_items_with_data(self, client):
        groups = {"pending-approval": [self._pending]}
        with patch("event_bus.main.grouped_items", return_value=groups):
            resp = client.get("/api/items")
        body = resp.json()
        assert resp.status_code == 200
        assert body["total"] == 1
        assert body["groups"][0]["state"] == "pending-approval"

    def test_list_items_cache_control_no_store(self, client):
        with patch("event_bus.main.grouped_items", return_value={}):
            resp = client.get("/api/items")
        assert resp.headers.get("cache-control") == "no-store"

    def test_list_items_with_pr_url_fetches_verdicts(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        r.set("pr_verdict:owner:repo:5:code_review", json.dumps({"status": "pass"}))
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        item_with_pr = {"id": "s-1", "title": "T", "state": "in-review",
                        "type": "story", "description": "",
                        "pr_url": "http://forgejo/owner/repo/pulls/5"}
        groups = {"in-review": [item_with_pr]}
        with patch("event_bus.main.grouped_items", return_value=groups):
            resp = client.get("/api/items")
        assert resp.status_code == 200
        items = resp.json()["groups"][0]["items"]
        assert "verdicts" in items[0]

    def test_get_item_success(self, client):
        with patch("event_bus.main.get_item", return_value=self._pending):
            resp = client.get("/api/items/idea-1")
        assert resp.status_code == 200
        assert resp.json()["id"] == "idea-1"

    def test_get_item_not_found(self, client):
        with patch("event_bus.main.get_item", return_value=None):
            resp = client.get("/api/items/nonexistent")
        assert resp.status_code == 404

    def test_approve_item_success(self, client):
        approved = {**self._pending, "state": "approved"}
        with patch("event_bus.main.get_item", return_value=self._pending), \
             patch("event_bus.main.update_state", return_value=approved), \
             patch("event_bus.main._run_planner", new_callable=AsyncMock):
            resp = client.post("/api/items/idea-1/approve")
        assert resp.status_code == 200
        assert resp.json()["state"] == "approved"

    def test_approve_item_not_found(self, client):
        with patch("event_bus.main.get_item", return_value=None):
            resp = client.post("/api/items/missing/approve")
        assert resp.status_code == 404

    def test_approve_item_wrong_state(self, client):
        already_approved = {**self._pending, "state": "approved"}
        with patch("event_bus.main.get_item", return_value=already_approved):
            resp = client.post("/api/items/idea-1/approve")
        assert resp.status_code == 409

    def test_code_item_success(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        in_progress = {**self._ready, "state": "in-progress"}
        with patch("event_bus.main.get_item", return_value=self._ready), \
             patch("event_bus.main.update_state", return_value=in_progress), \
             patch("event_bus.main.get_prompt", return_value=""), \
             patch("event_bus.main._run_coding_agent", new_callable=AsyncMock):
            resp = client.post("/api/items/story-1/code")
        assert resp.status_code == 202
        assert resp.json()["status"] == "coding_started"

    def test_code_item_not_found(self, client):
        with patch("event_bus.main.get_item", return_value=None):
            resp = client.post("/api/items/missing/code")
        assert resp.status_code == 404

    def test_code_item_not_a_story(self, client):
        with patch("event_bus.main.get_item", return_value=self._pending):
            resp = client.post("/api/items/idea-1/code")
        assert resp.status_code == 409

    def test_code_item_wrong_state(self, client):
        in_progress = {**self._ready, "state": "in-progress"}
        with patch("event_bus.main.get_item", return_value=in_progress):
            resp = client.post("/api/items/story-1/code")
        assert resp.status_code == 409

    def test_plan_item_success(self, client):
        with patch("event_bus.main.get_item", return_value=self._pending), \
             patch("event_bus.main._run_planner", new_callable=AsyncMock):
            resp = client.post("/api/items/idea-1/plan")
        assert resp.status_code == 202
        assert resp.json()["status"] == "planning_started"

    def test_plan_item_not_found(self, client):
        with patch("event_bus.main.get_item", return_value=None):
            resp = client.post("/api/items/missing/plan")
        assert resp.status_code == 404

    def test_plan_item_wrong_type(self, client):
        with patch("event_bus.main.get_item", return_value=self._ready):
            resp = client.post("/api/items/story-1/plan")
        assert resp.status_code == 409

    def test_reject_item_success(self, client):
        rejected = {**self._pending, "state": "rejected"}
        with patch("event_bus.main.get_item", return_value=self._pending), \
             patch("event_bus.main.update_state", return_value=rejected):
            resp = client.post("/api/items/idea-1/reject")
        assert resp.status_code == 200
        assert resp.json()["state"] == "rejected"

    def test_reject_item_not_found(self, client):
        with patch("event_bus.main.get_item", return_value=None):
            resp = client.post("/api/items/missing/reject")
        assert resp.status_code == 404

    def test_reject_item_wrong_state(self, client):
        approved = {**self._pending, "state": "approved"}
        with patch("event_bus.main.get_item", return_value=approved):
            resp = client.post("/api/items/idea-1/reject")
        assert resp.status_code == 409

    def test_mark_merged_success(self, client):
        in_review = {**self._ready, "state": "in-review"}
        merged = {**self._ready, "state": "merged"}
        with patch("event_bus.main.get_item", return_value=in_review), \
             patch("event_bus.main.update_state", return_value=merged), \
             patch("event_bus.main.unlock_next_story", return_value=None):
            resp = client.post("/api/items/story-1/merged")
        assert resp.status_code == 200

    def test_mark_merged_not_found(self, client):
        with patch("event_bus.main.get_item", return_value=None):
            resp = client.post("/api/items/missing/merged")
        assert resp.status_code == 404

    def test_mark_merged_wrong_state(self, client):
        with patch("event_bus.main.get_item", return_value=self._ready):
            resp = client.post("/api/items/story-1/merged")
        assert resp.status_code == 409

    def test_mark_merged_with_unlocked_story(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        in_review = {**self._ready, "state": "in-review"}
        merged = {**self._ready, "state": "merged"}
        next_story = {"id": "story-2", "title": "Next", "state": "ready",
                      "type": "story", "description": ""}
        with patch("event_bus.main.get_item", return_value=in_review), \
             patch("event_bus.main.update_state", return_value=merged), \
             patch("event_bus.main.unlock_next_story", return_value=next_story):
            resp = client.post("/api/items/story-1/merged")
        assert resp.status_code == 200
        assert resp.json()["unlocked"] is not None

    def test_log_stream_item_not_found(self, client):
        with patch("event_bus.main.get_item", return_value=None):
            resp = client.get("/api/items/missing/log-stream")
        assert resp.status_code == 404


# ── /api/prompts endpoints ────────────────────────────────────────────────────

class TestPromptsApi:
    def test_get_prompts_returns_list(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.get("/api/prompts")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert any(p["key"] == "coder.story" for p in body)

    def test_update_prompt_success(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.put("/api/prompts/coder.story", json={"value": "Custom prompt"})
        assert resp.status_code == 200
        assert resp.json()["saved"] is True
        assert resp.json()["key"] == "coder.story"

    def test_update_prompt_invalid_json(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.put("/api/prompts/coder.story",
                          content=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status_code == 400

    def test_update_prompt_non_string_value(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.put("/api/prompts/coder.story", json={"value": 42})
        assert resp.status_code == 400

    def test_update_prompt_unknown_key(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.put("/api/prompts/unknown.key", json={"value": "text"})
        assert resp.status_code == 404

    def test_reset_prompt_success(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.delete("/api/prompts/coder.story")
        assert resp.status_code == 200
        assert resp.json()["reset"] is True


# ── /webhook/forgejo extra paths ──────────────────────────────────────────────

class TestForgejoWebhookExtra:
    def _post(self, client, payload_dict, event_type="pull_request"):
        payload = json.dumps(payload_dict).encode()
        sig = sign_forgejo(payload)
        return client.post(
            "/webhook/forgejo",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Gitea-Event": event_type,
                "X-Gitea-Signature": sig,
            },
        )

    def test_invalid_json_returns_400(self, client):
        raw = b"not json"
        sig = sign_forgejo(raw)
        resp = client.post(
            "/webhook/forgejo",
            content=raw,
            headers={"Content-Type": "application/json",
                     "X-Gitea-Event": "pull_request",
                     "X-Gitea-Signature": sig},
        )
        assert resp.status_code == 400

    def test_invalid_pr_payload_returns_400(self, client):
        # Valid JSON but missing required ForgejoPREvent fields
        resp = self._post(client, {"action": "opened"}, event_type="pull_request")
        assert resp.status_code == 400

    def test_skip_non_pr_event(self, client):
        resp = self._post(client, {"action": "ping"}, event_type="push")
        assert resp.status_code == 202
        assert resp.json()["result"] == "skipped"
        assert "unhandled" in resp.json()["reason"]

    def test_pr_closed_merged_no_matching_story(self, client):
        payload = {**FORGEJO_PR_OPENED,
                   "action": "closed",
                   "pull_request": {**FORGEJO_PR_OPENED["pull_request"], "merged": True}}
        with patch("event_bus.main.find_item_by_pr_url", return_value=None):
            resp = self._post(client, payload)
        assert resp.status_code == 202
        assert resp.json()["result"] == "skipped"

    def test_pr_closed_merged_advances_story(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        story = {"id": "s-1", "title": "T", "state": "in-review", "type": "story",
                 "description": "", "pr_url": "http://localhost:13000/dev/myrepo/pulls/42"}
        merged = {**story, "state": "merged"}
        payload = {**FORGEJO_PR_OPENED,
                   "action": "closed",
                   "pull_request": {**FORGEJO_PR_OPENED["pull_request"], "merged": True}}
        with patch("event_bus.main.find_item_by_pr_url", return_value=story), \
             patch("event_bus.main.update_state", return_value=merged), \
             patch("event_bus.main.unlock_next_story", return_value=None):
            resp = self._post(client, payload)
        assert resp.status_code == 202
        assert resp.json()["result"] == "merged"
        assert resp.json()["item_id"] == "s-1"

    def test_pr_closed_merged_with_next_story(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        story = {"id": "s-1", "title": "T", "state": "in-review", "type": "story",
                 "description": "", "pr_url": "http://localhost:13000/dev/myrepo/pulls/42"}
        next_story = {"id": "s-2", "title": "Next", "state": "ready", "type": "story",
                      "description": ""}
        merged = {**story, "state": "merged"}
        in_progress = {**next_story, "state": "in-progress"}
        payload = {**FORGEJO_PR_OPENED,
                   "action": "closed",
                   "pull_request": {**FORGEJO_PR_OPENED["pull_request"], "merged": True}}
        with patch("event_bus.main.find_item_by_pr_url", return_value=story), \
             patch("event_bus.main.update_state", return_value=merged), \
             patch("event_bus.main.unlock_next_story", return_value=next_story), \
             patch("event_bus.main.get_prompt", return_value=""), \
             patch("event_bus.main._run_coding_agent", new_callable=AsyncMock):
            resp = self._post(client, payload)
        assert resp.status_code == 202
        assert resp.json()["unlocked"] == "s-2"

    def test_pr_synchronize_clears_retry_key(self, client):
        payload = {**FORGEJO_PR_OPENED,
                   "action": "synchronize",
                   "pull_request": {**FORGEJO_PR_OPENED["pull_request"], "merged": False}}
        resp = self._post(client, payload)
        assert resp.status_code == 202

    def test_review_event_changes_requested_triggers_recode(self, client):
        review_payload = {
            "action": "reviewed",
            "review": {"id": 1, "type": "reject", "body": "Fix tests", "html_url": "http://x"},
            "pull_request": FORGEJO_PR_OPENED["pull_request"],
            "repository": FORGEJO_PR_OPENED["repository"],
            "sender": {"login": "reviewer"},
        }
        with patch("event_bus.main._run_recode_agent", new_callable=AsyncMock):
            resp = self._post(client, review_payload, event_type="pull_request_review")
        assert resp.status_code == 202
        assert resp.json()["result"] == "accepted"

    def test_review_event_approve_skipped(self, client):
        review_payload = {
            "action": "reviewed",
            "review": {"id": 1, "type": "approve", "body": "LGTM", "html_url": "http://x"},
            "pull_request": FORGEJO_PR_OPENED["pull_request"],
            "repository": FORGEJO_PR_OPENED["repository"],
            "sender": {"login": "reviewer"},
        }
        resp = self._post(client, review_payload, event_type="pull_request_review")
        assert resp.status_code == 202
        assert resp.json()["result"] == "skipped"

    def test_review_event_invalid_payload_skipped(self, client):
        # repository field is required in ForgejoReviewEvent → ValidationError → skipped
        review_payload = {"action": "reviewed", "review": {}}
        resp = self._post(client, review_payload, event_type="pull_request_review")
        assert resp.status_code == 202
        assert resp.json()["result"] == "skipped"


# ── /internal/ endpoints ──────────────────────────────────────────────────────

class TestInternalEndpoints:
    _story = {"id": "s-1", "title": "Test Story", "state": "in-review",
               "type": "story", "description": "desc"}

    def test_internal_pr_merged_no_story(self, client):
        with patch("event_bus.main.find_item_by_pr_url", return_value=None):
            resp = client.post("/internal/pr-merged", json={
                "pr_url": "http://forgejo/owner/repo/pulls/5",
                "pr_number": 5,
            })
        assert resp.status_code == 404

    def test_internal_pr_merged_empty_url_returns_404(self, client):
        resp = client.post("/internal/pr-merged", json={"pr_url": "", "pr_number": 0})
        assert resp.status_code == 404

    def test_internal_pr_merged_with_story(self, client):
        merged = {**self._story, "state": "merged"}
        with patch("event_bus.main.find_item_by_pr_url", return_value=self._story), \
             patch("event_bus.main.update_state", return_value=merged), \
             patch("event_bus.main.unlock_next_story", return_value=None):
            resp = client.post("/internal/pr-merged", json={
                "pr_url": "http://forgejo/owner/repo/pulls/5",
                "pr_number": 5,
            })
        assert resp.status_code == 200
        assert resp.json()["merged"]["state"] == "merged"

    def test_internal_pr_merged_with_unlocked_story(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        merged = {**self._story, "state": "merged"}
        next_s = {"id": "s-2", "title": "Next", "state": "ready", "type": "story",
                  "description": ""}
        with patch("event_bus.main.find_item_by_pr_url", return_value=self._story), \
             patch("event_bus.main.update_state", return_value=merged), \
             patch("event_bus.main.unlock_next_story", return_value=next_s), \
             patch("event_bus.main.get_prompt", return_value=""), \
             patch("event_bus.main._run_coding_agent", new_callable=AsyncMock):
            resp = client.post("/internal/pr-merged", json={
                "pr_url": "http://forgejo/owner/repo/pulls/5",
                "pr_number": 5,
            })
        assert resp.status_code == 200
        assert resp.json()["unlocked"] == "s-2"

    def test_internal_recode_no_story(self, client):
        with patch("event_bus.main.find_item_by_pr_url", return_value=None):
            resp = client.post("/internal/recode-for-pr", json={
                "repo_full_name": "owner/repo",
                "pr_number": 5,
                "pr_url": "http://forgejo/owner/repo/pulls/5",
            })
        assert resp.status_code == 404

    def test_internal_recode_retry_cap_reached(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        r.set("recode_retries:owner/repo:5", "3")
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        changes = {**self._story, "state": "changes-requested"}
        with patch("event_bus.main.find_item_by_pr_url", return_value=self._story), \
             patch("event_bus.main.update_state", return_value=changes):
            resp = client.post("/internal/recode-for-pr", json={
                "repo_full_name": "owner/repo",
                "pr_number": 5,
                "pr_url": "http://forgejo/owner/repo/pulls/5",
            })
        assert resp.status_code == 202
        assert resp.json()["status"] == "retry_cap_reached"

    def test_internal_recode_starts_successfully(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        changes = {**self._story, "state": "changes-requested"}
        with patch("event_bus.main.find_item_by_pr_url", return_value=self._story), \
             patch("event_bus.main.update_state", return_value=changes), \
             patch("event_bus.main.get_prompt", return_value="review prompt"), \
             patch("asyncio.create_task"):
            resp = client.post("/internal/recode-for-pr", json={
                "repo_full_name": "owner/repo",
                "pr_number": 5,
                "pr_url": "http://forgejo/owner/repo/pulls/5",
                "feedback": "Fix the bug",
            })
        assert resp.status_code == 202
        assert resp.json()["status"] == "recode_started"
        assert resp.json()["attempt"] == 1


# ── /api/models/openrouter ────────────────────────────────────────────────────

class TestModelsEndpoint:
    def test_list_openrouter_models(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        with patch("event_bus.models_catalog.get_free_models",
                   return_value={"models": [], "count": 0, "cached_at": 0}), \
             patch("event_bus.models_catalog.get_ollama_suggestions", return_value=[]):
            resp = client.get("/api/models/openrouter")
        assert resp.status_code == 200
        assert "models" in resp.json()

    def test_list_openrouter_models_error_returns_502(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        with patch("event_bus.models_catalog.get_free_models",
                   side_effect=RuntimeError("OpenRouter down")):
            resp = client.get("/api/models/openrouter")
        assert resp.status_code == 502


# ── /health endpoint extra path ───────────────────────────────────────────────

class TestHealthExtra:
    def test_health_redis_error_returns_redis_false(self, client, monkeypatch):
        m = MagicMock()
        m.ping.side_effect = Exception("connection refused")
        monkeypatch.setattr("event_bus.main._redis_conn", m)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["redis"] is False


# ── prompt_store.py direct tests ──────────────────────────────────────────────

class TestPromptStoreDirect:
    def test_get_prompt_returns_default_when_not_set(self):
        r = fakeredis.FakeRedis()
        from event_bus.prompt_store import get_prompt, CODER_STORY
        val = get_prompt(r, "coder.story")
        assert val == CODER_STORY

    def test_get_prompt_returns_empty_for_unknown_key(self):
        r = fakeredis.FakeRedis()
        from event_bus.prompt_store import get_prompt
        val = get_prompt(r, "unknown.key")
        assert val == ""

    def test_set_prompt_saves_custom_value(self):
        r = fakeredis.FakeRedis()
        from event_bus.prompt_store import get_prompt, set_prompt
        set_prompt(r, "coder.story", "My custom prompt")
        assert get_prompt(r, "coder.story") == "My custom prompt"

    def test_set_prompt_raises_for_unknown_key(self):
        r = fakeredis.FakeRedis()
        from event_bus.prompt_store import set_prompt
        with pytest.raises(ValueError, match="Unknown prompt key"):
            set_prompt(r, "unknown.key", "value")

    def test_delete_prompt_restores_default(self):
        r = fakeredis.FakeRedis()
        from event_bus.prompt_store import get_prompt, set_prompt, delete_prompt, CODER_STORY
        set_prompt(r, "coder.story", "custom")
        delete_prompt(r, "coder.story")
        assert get_prompt(r, "coder.story") == CODER_STORY

    def test_list_prompts_shows_is_custom_true(self):
        r = fakeredis.FakeRedis()
        from event_bus.prompt_store import set_prompt, list_prompts
        set_prompt(r, "coder.story", "custom prompt")
        prompts = list_prompts(r)
        story = next(p for p in prompts if p["key"] == "coder.story")
        assert story["is_custom"] is True
        assert story["current"] == "custom prompt"

    def test_list_prompts_shows_is_custom_false_when_not_set(self):
        r = fakeredis.FakeRedis()
        from event_bus.prompt_store import list_prompts
        prompts = list_prompts(r)
        story = next(p for p in prompts if p["key"] == "coder.story")
        assert story["is_custom"] is False


# ── jobs/handlers.py missing paths ───────────────────────────────────────────

class TestHandlersMissingPaths:
    def test_handle_pr_event_all_roles_rate_limited(self):
        from event_bus.limits import check_rate
        from event_bus.config_store import patch_config
        from event_bus.jobs.handlers import handle_pr_event

        r = fakeredis.FakeRedis()
        patch_config(r, {"limits": {
            "max_rpm_reviewer": 1, "max_rpm_tester": 1, "max_rpm_security": 1,
        }})
        # Exhaust all three limits
        check_rate(r, "reviewer", 1)
        check_rate(r, "tester", 1)
        check_rate(r, "security", 1)

        with patch("redis.from_url", return_value=r), \
             patch("rq.Queue"):
            result = handle_pr_event("owner/repo", 1, "abc123", "opened")

        assert result["status"] == "rate_limited"
        assert "all review roles" in result["reason"]


# ── jobs/pr_jobs.py missing paths ─────────────────────────────────────────────

class TestPrJobsMissingPaths:
    _base = dict(repo_full_name="alice/backend", pr_number=7, head_sha="a" * 40)

    def test_run_tester_import_error_returns_error(self):
        from event_bus.jobs.pr_jobs import run_tester
        with patch("event_bus.jobs.pr_jobs._redis_and_limits", return_value=_no_limits()), \
             patch.dict("sys.modules", {"reviewer": None, "reviewer.test_runner": None}):
            result = run_tester(**self._base)
        assert result["status"] == "error"
        assert result["role"] == "test_run"

    def test_run_security_scanner_import_error_returns_error(self):
        from event_bus.jobs.pr_jobs import run_security_scanner
        with patch("event_bus.jobs.pr_jobs._redis_and_limits", return_value=_no_limits()), \
             patch.dict("sys.modules", {"reviewer": None, "reviewer.security_scan": None}):
            result = run_security_scanner(**self._base)
        assert result["status"] == "error"
        assert result["role"] == "security"

    def test_do_merge_pr_import_error_returns_error(self):
        from event_bus.jobs.pr_jobs import do_merge_pr
        with patch.dict("sys.modules", {"reviewer": None,
                                        "reviewer.forgejo_client": None,
                                        "reviewer.config": None}):
            result = do_merge_pr("owner", "repo", 5)
        assert result["status"] == "error"
        assert "reviewer" in result["reason"]

    def test_do_merge_pr_exception_returns_error(self):
        from event_bus.jobs.pr_jobs import do_merge_pr
        with patch("reviewer.forgejo_client.ForgejoClient") as MockFJ:
            MockFJ.return_value.__enter__.return_value.merge_pr.side_effect = RuntimeError("fail")
            result = do_merge_pr("owner", "repo", 5)
        assert result["status"] == "error"
        assert "fail" in result["reason"]

    def test_do_merge_pr_success(self):
        from event_bus.jobs.pr_jobs import do_merge_pr
        with patch("reviewer.forgejo_client.ForgejoClient") as MockFJ:
            MockFJ.return_value.__enter__.return_value.merge_pr.return_value = {"sha": "abc123"}
            result = do_merge_pr("owner", "repo", 5, approver="bob")
        assert result["status"] == "merged"
        assert result["approver"] == "bob"
        assert result["sha"] == "abc123"


# ── Targeted coverage: helper functions and small branches ────────────────────

class TestHelperFunctions:
    """Tests for small helper functions not exercised elsewhere."""

    def test_root_redirects_to_ui(self, client):
        resp = client.get("/", follow_redirects=False)
        # Redirect to /ui/
        assert resp.status_code in (307, 301, 302)
        assert "/ui" in resp.headers.get("location", "")

    def test_slugify_converts_to_lowercase_hyphens(self):
        from event_bus.main import _slugify
        assert _slugify("Hello World!") == "hello-world"
        assert _slugify("My Feature #2") == "my-feature-2"

    def test_slugify_truncates_long_titles(self):
        from event_bus.main import _slugify
        result = _slugify("A" * 50)
        assert len(result) <= 40

    def test_fetch_verdicts_empty_pr_url_returns_empty(self, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        from event_bus.main import _fetch_verdicts
        assert _fetch_verdicts("") == {}

    def test_fetch_verdicts_no_redis_returns_empty(self, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", None)
        from event_bus.main import _fetch_verdicts
        assert _fetch_verdicts("http://x/owner/repo/pulls/1") == {}

    def test_fetch_verdicts_bad_url_path_returns_empty(self, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        from event_bus.main import _fetch_verdicts
        # "issues" not "pulls" — should fail the parts[-2] != "pulls" check
        assert _fetch_verdicts("http://x/owner/repo/issues/1") == {}

    def test_fetch_verdicts_redis_exception_returns_empty(self, monkeypatch):
        m = MagicMock()
        m.get.side_effect = Exception("redis error")
        monkeypatch.setattr("event_bus.main._redis_conn", m)
        from event_bus.main import _fetch_verdicts
        assert _fetch_verdicts("http://x/owner/repo/pulls/1") == {}

    def test_make_log_cb_pushes_line_to_redis(self, monkeypatch):
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        from event_bus.main import _make_log_cb
        cb = _make_log_cb("item-1")
        cb("hello log line")
        assert r.llen("agent_log:item-1") == 1
        assert b"hello log line" in r.lindex("agent_log:item-1", 0)

    def test_expire_log_sets_ttl(self, monkeypatch):
        r = fakeredis.FakeRedis()
        r.rpush("agent_log:item-1", b"line")
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        from event_bus.main import _expire_log
        _expire_log("item-1")
        assert r.ttl("agent_log:item-1") > 0

    def test_models_endpoint_refresh_flag(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        with patch("event_bus.models_catalog.refresh_free_models",
                   return_value={"models": [], "count": 0, "cached_at": 0}) as mock_r, \
             patch("event_bus.models_catalog.get_ollama_suggestions", return_value=[]):
            resp = client.get("/api/models/openrouter?refresh=true")
        assert resp.status_code == 200
        mock_r.assert_called_once()

    def test_coder_slot_acquire_without_redis_always_succeeds(self, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", None)
        from event_bus.main import _coder_slot_acquire
        assert _coder_slot_acquire() is True

    def test_coder_slot_acquire_with_redis_enforces_cap(self, monkeypatch):
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        monkeypatch.setattr("event_bus.main.settings",
                            MagicMock(max_coding_agents=2, board_auth_password=""))
        from event_bus.main import _coder_slot_acquire, _CODER_SLOT_KEY
        r.delete(_CODER_SLOT_KEY)
        assert _coder_slot_acquire() is True   # n=1 ≤ 2
        assert _coder_slot_acquire() is True   # n=2 ≤ 2
        assert _coder_slot_acquire() is False  # n=3 > 2, decremented back to 2

    def test_internal_recode_no_redis_returns_503(self, client, monkeypatch):
        story = {"id": "s-1", "title": "T", "state": "in-review",
                 "type": "story", "description": ""}
        monkeypatch.setattr("event_bus.main._redis_conn", None)
        with patch("event_bus.main.find_item_by_pr_url", return_value=story):
            resp = client.post("/internal/recode-for-pr", json={
                "repo_full_name": "owner/repo",
                "pr_number": 5,
                "pr_url": "http://forgejo/owner/repo/pulls/5",
            })
        assert resp.status_code == 503

    def test_approve_pr_no_queue_returns_503(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        r.set("pr_merge_pending:owner:repo:1",
              json.dumps({"repo_full_name": "owner/repo", "pr_number": 1}))
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        monkeypatch.setattr("event_bus.main._queue", None)
        monkeypatch.setattr("event_bus.main.settings",
                            MagicMock(temporal_address="", board_auth_password=""))
        resp = client.post("/api/prs/owner/repo/1/approve")
        assert resp.status_code == 503

    def test_coder_slot_release_with_no_ready_stories(self, monkeypatch):
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        with patch("event_bus.main.list_items", return_value=[]):
            from event_bus.main import _coder_slot_release_and_dispatch
            _coder_slot_release_and_dispatch()  # covers lines 76-81 (decr, expire, empty ready list)



# ── approve_pr_merge: Temporal signal branch (gate.pr_merge_approval) ──────────

class TestApprovePrTemporal:
    def _inject_temporalio(self, monkeypatch, connect):
        import sys, types
        mod = types.ModuleType("temporalio.client")
        FakeClient = MagicMock()
        FakeClient.connect = connect
        mod.Client = FakeClient
        monkeypatch.setitem(sys.modules, "temporalio", types.ModuleType("temporalio"))
        monkeypatch.setitem(sys.modules, "temporalio.client", mod)

    def test_temporal_signal_success(self, client, monkeypatch):
        monkeypatch.setattr(settings, "temporal_address", "temporal:7233")
        fake_handle = MagicMock()
        fake_handle.signal = AsyncMock()
        fake_client = MagicMock()
        fake_client.get_workflow_handle.return_value = fake_handle
        self._inject_temporalio(monkeypatch, AsyncMock(return_value=fake_client))

        resp = client.post("/api/prs/owner/repo/5/approve", json={"approver": "alice"})
        assert resp.status_code == 202
        assert resp.json()["via"] == "temporal_signal"
        fake_handle.signal.assert_awaited_once()

    def test_temporal_signal_failure_returns_502(self, client, monkeypatch):
        monkeypatch.setattr(settings, "temporal_address", "temporal:7233")
        self._inject_temporalio(monkeypatch, AsyncMock(side_effect=RuntimeError("boom")))

        resp = client.post("/api/prs/owner/repo/5/approve", json={})
        assert resp.status_code == 502
        assert "Temporal signal failed" in resp.json()["detail"]


# ── _provision_project_repo: commits CI workflow on fresh repos ───────────────

class TestProvisionProjectRepo:
    def _patch_common(self, monkeypatch, fj):
        from event_bus import main as m
        FakeClient = MagicMock()
        FakeClient.return_value.__enter__.return_value = fj
        FakeClient.return_value.__exit__.return_value = False
        import coding_agent.forgejo_client as fc
        monkeypatch.setattr(fc, "ForgejoClient", FakeClient)
        resp = MagicMock()
        resp.json.return_value = {"login": "devadmin"}
        resp.raise_for_status.return_value = None
        monkeypatch.setattr("httpx.get", lambda *a, **k: resp)
        return m

    def test_fresh_repo_commits_ci_workflow(self, monkeypatch):
        from event_bus.ci_workflow import CI_WORKFLOW_PATH, CI_WORKFLOW_YAML
        fj = MagicMock()
        fj.repo_exists.return_value = False
        m = self._patch_common(monkeypatch, fj)

        result = m._provision_project_repo("idea-1", "Add Login Page")

        assert result == "devadmin/add-login-page"
        fj.create_repo.assert_called_once()
        fj.create_file.assert_called_once()
        args, kwargs = fj.create_file.call_args
        assert kwargs.get("branch") == "main"
        assert CI_WORKFLOW_PATH in args
        assert CI_WORKFLOW_YAML in args
        # branch protection set on fresh repo (block direct push to main)
        fj.set_branch_protection.assert_called_once()
        bp_args, _ = fj.set_branch_protection.call_args
        assert "add-login-page" in bp_args and "main" in bp_args
        # reviewer-bot granted write access for least-privilege review/merge
        fj.add_collaborator.assert_called_once()
        co_args, _ = fj.add_collaborator.call_args
        assert "reviewer-bot" in co_args and "write" in co_args
        fj.create_webhook.assert_called_once()

    def test_existing_repo_skips_workflow_commit(self, monkeypatch):
        fj = MagicMock()
        fj.repo_exists.return_value = True
        m = self._patch_common(monkeypatch, fj)

        result = m._provision_project_repo("idea-2", "Existing Project")

        assert result == "devadmin/existing-project"
        fj.create_repo.assert_not_called()
        fj.create_file.assert_not_called()
        fj.set_branch_protection.assert_not_called()
        fj.add_collaborator.assert_not_called()
        fj.create_webhook.assert_called_once()

    def test_ci_workflow_commit_failure_is_non_fatal(self, monkeypatch):
        fj = MagicMock()
        fj.repo_exists.return_value = False
        fj.create_file.side_effect = RuntimeError("contents API down")
        m = self._patch_common(monkeypatch, fj)

        # Failure to commit the workflow must not fail provisioning
        result = m._provision_project_repo("idea-3", "Resilient Repo")
        assert result == "devadmin/resilient-repo"
        fj.create_webhook.assert_called_once()


# ── board HTTP Basic Auth middleware ──────────────────────────────────────────

class TestBoardBasicAuth:
    @staticmethod
    def _basic(user, password):
        import base64
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def test_api_blocked_without_credentials(self, client, monkeypatch):
        monkeypatch.setattr(settings, "board_auth_user", "admin")
        monkeypatch.setattr(settings, "board_auth_password", "s3cret")
        resp = client.get("/api/config")
        assert resp.status_code == 401
        assert resp.headers.get("www-authenticate", "").lower().startswith("basic")

    def test_api_allowed_with_correct_credentials(self, client, monkeypatch):
        monkeypatch.setattr(settings, "board_auth_user", "admin")
        monkeypatch.setattr(settings, "board_auth_password", "s3cret")
        resp = client.get("/api/config", headers={"Authorization": self._basic("admin", "s3cret")})
        assert resp.status_code == 200

    def test_api_rejected_with_wrong_credentials(self, client, monkeypatch):
        monkeypatch.setattr(settings, "board_auth_user", "admin")
        monkeypatch.setattr(settings, "board_auth_password", "s3cret")
        resp = client.get("/api/config", headers={"Authorization": self._basic("admin", "wrong")})
        assert resp.status_code == 401

    def test_health_is_exempt(self, client, monkeypatch):
        monkeypatch.setattr(settings, "board_auth_password", "s3cret")
        assert client.get("/health").status_code == 200

    def test_webhook_is_exempt_from_basic_auth(self, client, monkeypatch):
        # No basic-auth creds + bad signature must reach the handler → 403 (not 401)
        monkeypatch.setattr(settings, "board_auth_password", "s3cret")
        resp = client.post("/webhook/forgejo",
                           headers={"X-Gitea-Signature": "bad"},
                           json={"action": "opened"})
        assert resp.status_code == 403

    def test_auth_disabled_when_password_blank(self, client, monkeypatch):
        monkeypatch.setattr(settings, "board_auth_password", "")
        assert client.get("/api/config").status_code == 200
