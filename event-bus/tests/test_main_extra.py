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
             patch("event_bus.main.get_repo_for_story", return_value="dev/repo"), \
             patch("event_bus.main._await_post_merge_ci", new_callable=AsyncMock):
            resp = client.post("/api/items/story-1/merged")
        assert resp.status_code == 200
        assert resp.json()["post_merge_ci"] == "pending"

    def test_mark_merged_not_found(self, client):
        with patch("event_bus.main.get_item", return_value=None):
            resp = client.post("/api/items/missing/merged")
        assert resp.status_code == 404

    def test_mark_merged_wrong_state(self, client):
        with patch("event_bus.main.get_item", return_value=self._ready):
            resp = client.post("/api/items/story-1/merged")
        assert resp.status_code == 409

    def test_mark_merged_is_transient_pending_ci(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        in_review = {**self._ready, "state": "in-review"}
        merged = {**self._ready, "state": "merged"}
        with patch("event_bus.main.get_item", return_value=in_review), \
             patch("event_bus.main.update_state", return_value=merged), \
             patch("event_bus.main.get_repo_for_story", return_value="dev/repo"), \
             patch("event_bus.main._await_post_merge_ci", new_callable=AsyncMock) as await_ci:
            resp = client.post("/api/items/story-1/merged")
        assert resp.status_code == 200
        # unlock no longer happens at merge time; the post-merge CI gate drives it
        await_ci.assert_called_once()

    def test_log_stream_item_not_found(self, client):
        with patch("event_bus.main.get_item", return_value=None):
            resp = client.get("/api/items/missing/log-stream")
        assert resp.status_code == 404


# ── Operator recovery controls ────────────────────────────────────────────────

class TestRecoveryControls:
    _story_cr = {"id": "story-1", "title": "Add login", "state": "changes-requested",
                 "type": "story", "description": "desc", "repo": "dev/app",
                 "pr_url": "http://forge/dev/app/pulls/7"}
    _story_ip = {"id": "story-2", "title": "Add logout", "state": "in-progress",
                 "type": "story", "description": "desc", "pr_url": None}
    _idea = {"id": "idea-1", "title": "Auth", "state": "approved", "type": "idea",
             "description": "d", "pr_url": None}

    def test_retry_requeues_checks_and_clears_caps(self, client, mock_queue, monkeypatch):
        r = fakeredis.FakeRedis()
        r.set("recode_retries:dev/app:7", "3")
        r.set("post_merge_fix:story-1", "2")
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        pr = {"head": {"sha": "sha1", "ref": "story/login"}, "base": {"ref": "main"}}
        fake_fg = MagicMock()
        fake_fg.__enter__ = lambda s: s
        fake_fg.__exit__ = MagicMock(return_value=False)
        fake_fg.get.return_value = pr
        with patch("event_bus.main.get_item", return_value=self._story_cr), \
             patch("event_bus.main.update_state", return_value={**self._story_cr, "state": "in-review"}), \
             patch("event_bus.main._post_pr_comment"), \
             patch("coding_agent.forgejo_client.ForgejoClient", return_value=fake_fg):
            resp = client.post("/api/items/story-1/retry")
        assert resp.status_code == 202
        assert resp.json()["status"] == "retry_requeued"
        # caps cleared and the PR-review job re-enqueued
        assert r.get("recode_retries:dev/app:7") is None
        assert r.get("post_merge_fix:story-1") is None
        assert mock_queue.enqueue.called  # PR-review job re-enqueued

    def test_retry_without_pr_is_409(self, client):
        no_pr = {**self._story_cr, "pr_url": None}
        with patch("event_bus.main.get_item", return_value=no_pr):
            resp = client.post("/api/items/story-1/retry")
        assert resp.status_code == 409

    def test_reset_restarts_coder(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        with patch("event_bus.main.get_item", return_value=self._story_ip), \
             patch("event_bus.main.update_state", return_value={**self._story_ip, "state": "in-progress"}), \
             patch("event_bus.main.get_prompt", return_value=""), \
             patch("event_bus.main._run_coding_agent", new_callable=AsyncMock) as coder:
            resp = client.post("/api/items/story-2/reset")
        assert resp.status_code == 202
        assert resp.json()["status"] == "reset_and_coding"
        coder.assert_called_once()

    def test_abandon_marks_and_unlocks_next(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        with patch("event_bus.main.get_item", return_value=self._story_cr), \
             patch("event_bus.main.update_state", return_value={**self._story_cr, "state": "abandoned"}) as upd, \
             patch("event_bus.main.unlock_next_story") as unlock, \
             patch("event_bus.main._dispatch_next_ready"):
            resp = client.post("/api/items/story-1/abandon")
        assert resp.status_code == 200
        assert resp.json()["state"] == "abandoned"
        upd.assert_any_call("story-1", "abandoned")
        unlock.assert_called_once_with("story-1")

    def test_abandon_already_terminal_is_409(self, client):
        with patch("event_bus.main.get_item", return_value={**self._story_cr, "state": "done"}):
            resp = client.post("/api/items/story-1/abandon")
        assert resp.status_code == 409

    def test_cancel_idea_abandons_open_stories(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        children = [
            {"id": "s1", "parent_id": "idea-1", "type": "story", "state": "in-review", "pr_url": None},
            {"id": "s2", "parent_id": "idea-1", "type": "story", "state": "done", "pr_url": None},
            {"id": "s3", "parent_id": "other", "type": "story", "state": "ready", "pr_url": None},
        ]
        calls = []
        def _upd(item_id, state):
            calls.append((item_id, state)); return {**self._idea, "state": state}
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=children), \
             patch("event_bus.main.update_state", side_effect=_upd):
            resp = client.post("/api/items/idea-1/cancel")
        assert resp.status_code == 200
        # only the open child of THIS idea is abandoned; done child + other idea untouched
        assert ("s1", "abandoned") in calls
        assert ("s2", "abandoned") not in calls
        assert ("s3", "abandoned") not in calls
        assert ("idea-1", "abandoned") in calls

    def test_cancel_non_idea_is_409(self, client):
        with patch("event_bus.main.get_item", return_value=self._story_cr):
            resp = client.post("/api/items/story-1/cancel")
        assert resp.status_code == 409


class TestDesignDecisions:
    def test_normalize_decisions_shapes_and_caps(self):
        from event_bus.main import _normalize_decisions
        raw = [{"question": f"Q{i}", "recommended": "A", "rationale": "r",
                "alternatives": ["B", "C"]} for i in range(9)]
        raw.append({"no_question": True})  # dropped
        out = _normalize_decisions(raw)
        assert len(out) == 7                     # capped
        assert out[0] == {"id": "d0", "question": "Q0", "recommended": "A",
                          "rationale": "r", "alternatives": ["B", "C"], "chosen": None}

    def test_format_decisions_renders_chosen(self):
        from event_bus.main import _format_decisions
        import json as _j
        decisions = _j.dumps([
            {"id": "d0", "question": "Storage?", "chosen": "Postgres"},
            {"id": "d1", "question": "Auth?", "chosen": ""},   # unanswered → skipped
        ])
        block = _format_decisions(decisions)
        assert "LOCKED" in block and "Storage? → Postgres" in block and "Auth?" not in block

    def test_approve_merges_answers_and_model(self, client, monkeypatch):
        import json as _j
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        idea = {"id": "idea-1", "title": "P", "state": "pending-approval", "type": "idea",
                "description": "d", "stack": "python", "sdlc": "standard",
                "design_decisions": _j.dumps([
                    {"id": "d0", "question": "Storage?", "recommended": "SQLite",
                     "alternatives": ["Postgres"], "chosen": None},
                    {"id": "d1", "question": "Auth?", "recommended": "JWT",
                     "alternatives": [], "chosen": None}])}
        with patch("event_bus.main.get_item", return_value=idea), \
             patch("event_bus.main.update_state", return_value={**idea, "state": "approved"}), \
             patch("event_bus.main.set_planning_inputs") as spi, \
             patch("event_bus.main._run_planner", new_callable=AsyncMock):
            resp = client.post("/api/items/idea-1/approve", json={
                "design_answers": {"d0": "Postgres"},        # d1 left to auto-accept
                "planner_model": "openrouter/anthropic/claude-sonnet-4-6"})
        assert resp.status_code == 200
        kw = spi.call_args.kwargs
        merged = _j.loads(kw["design_decisions"])
        assert merged[0]["chosen"] == "Postgres"      # operator's answer
        assert merged[1]["chosen"] == "JWT"           # unanswered → recommendation
        assert kw["planner_model"] == "openrouter/anthropic/claude-sonnet-4-6"


class TestPlanApproval:
    _idea = {"id": "idea-1", "title": "P", "state": "approved", "type": "idea",
             "description": "d", "pr_url": None}
    _backlog = [
        {"id": "s1", "parent_id": "idea-1", "type": "story", "state": "backlog",
         "sequence": 1, "title": "One", "description": "d"},
        {"id": "s2", "parent_id": "idea-1", "type": "story", "state": "backlog", "sequence": 2},
    ]

    def test_approve_plan_starts_first_story(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=self._backlog), \
             patch("event_bus.main.update_state") as upd, \
             patch("event_bus.main.get_prompt", return_value=""), \
             patch("event_bus.main._run_coding_agent", new_callable=AsyncMock) as coder:
            resp = client.post("/api/items/idea-1/approve-plan")
        assert resp.status_code == 202
        assert resp.json()["first_story"] == "s1"
        upd.assert_any_call("s1", "in-progress")   # lowest-sequence story dispatched
        coder.assert_called_once()

    def test_approve_plan_already_started_is_409(self, client):
        started = [{**self._backlog[0], "state": "in-progress"}, self._backlog[1]]
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=started):
            resp = client.post("/api/items/idea-1/approve-plan")
        assert resp.status_code == 409

    def test_approve_plan_no_stories_is_409(self, client):
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=[]):
            resp = client.post("/api/items/idea-1/approve-plan")
        assert resp.status_code == 409


class TestResumeImport:
    _idea = {"id": "imp-1", "title": "P", "state": "approved", "type": "idea",
             "prompt": "## Epic 1\nspec\n## Epic 2\nspec", "stack": "python", "sdlc": "standard"}
    _backlog = [
        {"id": "s1", "parent_id": "imp-1", "type": "story", "state": "backlog",
         "sequence": 1, "title": "One", "epic": "Epic 1"},
    ]

    def test_resume_triggers_background_fill(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=self._backlog), \
             patch("event_bus.main._resume_import", new_callable=AsyncMock) as res:
            resp = client.post("/api/items/imp-1/resume-import")
        assert resp.status_code == 202
        assert resp.json()["existing_stories"] == 1
        res.assert_called_once_with("imp-1")

    def test_resume_rejected_once_plan_started(self, client):
        started = [{**self._backlog[0], "state": "in-progress"}]
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=started):
            resp = client.post("/api/items/imp-1/resume-import")
        assert resp.status_code == 409

    def test_resume_rejected_without_source_plan(self, client):
        with patch("event_bus.main.get_item", return_value={**self._idea, "prompt": ""}):
            resp = client.post("/api/items/imp-1/resume-import")
        assert resp.status_code == 409


class TestProjectLifecycle:
    _idea = {"id": "idea-1", "title": "Auth", "state": "approved", "type": "idea",
             "repo": "dev/app", "description": "d", "pr_url": None}
    _done = [{"id": "s1", "parent_id": "idea-1", "type": "story", "state": "done"}]
    _busy = [{"id": "s1", "parent_id": "idea-1", "type": "story", "state": "in-review"}]

    def test_archive_settled_project(self, client):
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=self._done), \
             patch("event_bus.main.set_archived", return_value=2) as arch:
            resp = client.post("/api/items/idea-1/archive")
        assert resp.status_code == 200 and resp.json()["status"] == "archived"
        arch.assert_called_once_with("idea-1", True)

    def test_archive_blocked_while_in_flight(self, client):
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=self._busy):
            resp = client.post("/api/items/idea-1/archive")
        assert resp.status_code == 409

    def test_archive_non_idea_is_409(self, client):
        story = {"id": "s1", "type": "story", "state": "done"}
        with patch("event_bus.main.get_item", return_value=story):
            resp = client.post("/api/items/s1/archive")
        assert resp.status_code == 409

    def test_unarchive(self, client):
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.set_archived", return_value=2) as arch:
            resp = client.post("/api/items/idea-1/unarchive")
        assert resp.status_code == 200 and resp.json()["status"] == "restored"
        arch.assert_called_once_with("idea-1", False)

    def test_delete_items_only_keeps_repo(self, client):
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=self._done), \
             patch("event_bus.main.delete_item_tree", return_value=2) as dele, \
             patch("event_bus.main._delete_forgejo_repo") as delrepo:
            resp = client.delete("/api/items/idea-1")
        assert resp.status_code == 200
        assert resp.json()["repo_deleted"] is None
        dele.assert_called_once_with("idea-1")
        delrepo.assert_not_called()

    def test_delete_with_repo(self, client):
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=self._done), \
             patch("event_bus.main.delete_item_tree", return_value=2), \
             patch("event_bus.main._delete_forgejo_repo", return_value=True) as delrepo:
            resp = client.delete("/api/items/idea-1?delete_repo=true")
        assert resp.status_code == 200 and resp.json()["repo_deleted"] is True
        delrepo.assert_called_once_with("dev/app")

    def test_delete_blocked_while_in_flight(self, client):
        with patch("event_bus.main.get_item", return_value=self._idea), \
             patch("event_bus.main.list_items", return_value=self._busy):
            resp = client.delete("/api/items/idea-1")
        assert resp.status_code == 409

    def test_list_projects_endpoint(self, client):
        projects = [{"id": "idea-1", "title": "Auth", "story_count": 4,
                     "stories_done": 4, "archived": False}]
        with patch("event_bus.main.list_projects", return_value=projects):
            resp = client.get("/api/projects")
        assert resp.status_code == 200
        assert resp.json()["projects"][0]["stories_done"] == 4


class TestSandboxResilience:
    def test_read_container_json_from_archive(self):
        import io, tarfile
        payload = json.dumps({"status": "success", "pr_url": "x"}).encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="result.json"); info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        container = MagicMock()
        container.get_archive.return_value = (iter([buf.getvalue()]), {})
        from event_bus.main import _read_container_json
        assert _read_container_json(container, "/output/result.json") == {"status": "success", "pr_url": "x"}

    def test_read_container_json_missing_returns_none(self):
        container = MagicMock()
        container.get_archive.side_effect = Exception("not found")
        from event_bus.main import _read_container_json
        assert _read_container_json(container, "/x") is None

    def test_reconcile_orphaned_stories_resets_and_dispatches(self, monkeypatch):
        monkeypatch.setattr(settings, "max_coding_agents", 2)
        stories = [{"id": "s1", "state": "in-progress", "type": "story"}]
        with patch("event_bus.main.list_items", return_value=stories), \
             patch("event_bus.main.update_state") as upd, \
             patch("event_bus.main._dispatch_next_ready") as disp:
            from event_bus.main import _reconcile_orphaned_stories
            _reconcile_orphaned_stories()
        upd.assert_any_call("s1", "ready")
        assert disp.call_count == 2  # fills both coder slots

    def test_reconcile_orphaned_stories_noop_when_none(self, monkeypatch):
        with patch("event_bus.main.list_items", return_value=[]), \
             patch("event_bus.main._dispatch_next_ready") as disp:
            from event_bus.main import _reconcile_orphaned_stories
            _reconcile_orphaned_stories()
        disp.assert_not_called()

    def test_reap_orphan_sandboxes_process_mode_noop(self, monkeypatch):
        monkeypatch.setattr(settings, "sandbox_mode", "process")
        from event_bus.main import _reap_orphan_coder_sandboxes
        assert _reap_orphan_coder_sandboxes() == 0

    def test_reap_orphan_sandboxes_removes_labeled(self, monkeypatch):
        import sys
        monkeypatch.setattr(settings, "sandbox_mode", "docker")
        c1, c2 = MagicMock(), MagicMock()
        fake_client = MagicMock()
        fake_client.containers.list.return_value = [c1, c2]
        fake_docker = MagicMock()
        fake_docker.from_env.return_value = fake_client
        monkeypatch.setitem(sys.modules, "docker", fake_docker)
        from event_bus.main import _reap_orphan_coder_sandboxes
        assert _reap_orphan_coder_sandboxes() == 2
        c1.remove.assert_called_once_with(force=True)
        # only the coder-labeled containers are targeted
        assert fake_client.containers.list.call_args[1]["filters"] == {"label": "dev-agents.sandbox=true"}


class TestImportPlan:
    def test_import_creates_approved_idea_and_normalizes(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        monkeypatch.setattr("event_bus.main.over_budget", lambda *a, **k: False)
        fake = {"id": "imp-1", "title": "Imported plan", "state": "approved", "type": "idea"}
        with patch("event_bus.main.create_item", return_value=fake) as ci, \
             patch("event_bus.main._run_import", new_callable=AsyncMock) as ri:
            resp = client.post("/api/ideas/import", json={
                "plan_text": "## Epic 1 — Data\n### Story 1.1 build the model with a spec " * 3,
                "stack": "python", "sdlc": "standard",
                "planner_model": "openrouter/anthropic/claude-sonnet-4-6"})
        assert resp.status_code == 202
        kw = ci.call_args.kwargs
        assert kw["state"] == "approved" and kw["stack"] == "python"
        assert "Epic 1" in kw["prompt"]                      # the plan is stored verbatim
        assert kw["planner_model"] == "openrouter/anthropic/claude-sonnet-4-6"
        ri.assert_called_once()

    def test_import_rejects_short_plan(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.post("/api/ideas/import", json={"plan_text": "too short"})
        assert resp.status_code == 400

    def test_import_rejects_unknown_stack(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        monkeypatch.setattr("event_bus.main.over_budget", lambda *a, **k: False)
        resp = client.post("/api/ideas/import", json={"plan_text": "x" * 100, "stack": "cobol"})
        assert resp.status_code == 422


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
             patch("event_bus.main.get_repo_for_story", return_value="dev/myrepo"), \
             patch("event_bus.main._await_post_merge_ci", new_callable=AsyncMock):
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
             patch("event_bus.main.get_repo_for_story", return_value="dev/myrepo"), \
             patch("event_bus.main._await_post_merge_ci", new_callable=AsyncMock) as await_ci:
            resp = self._post(client, payload)
        assert resp.status_code == 202
        assert resp.json()["result"] == "merged"
        await_ci.assert_called_once()

    def test_human_push_resets_retry_key_but_coder_bot_does_not(self, client, monkeypatch):
        # A human's commit resets the recode cap (fresh attempts); the coder-bot's own
        # recode commits must NOT, or the cap never fires and recodes loop forever.
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        from event_bus.config import settings as _s
        monkeypatch.setattr(_s, "forgejo_coder_user", "coder-bot")
        key = "recode_retries:dev/myrepo:42"
        base = {**FORGEJO_PR_OPENED, "action": "synchronized",
                "pull_request": {**FORGEJO_PR_OPENED["pull_request"], "merged": False}}

        r.set(key, "3")
        assert self._post(client, {**base, "sender": {"login": "alice"}}).status_code == 202
        assert r.get(key) is None  # human push -> reset

        r.set(key, "3")
        assert self._post(client, {**base, "sender": {"login": "coder-bot"}}).status_code == 202
        assert r.get(key) == b"3"  # coder-bot recode commit -> NOT reset

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

    def test_internal_pr_merged_sets_merged_pending_ci(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        merged = {**self._story, "state": "merged"}
        with patch("event_bus.main.find_item_by_pr_url", return_value=self._story), \
             patch("event_bus.main.update_state", return_value=merged), \
             patch("event_bus.main.get_repo_for_story", return_value="owner/repo"), \
             patch("event_bus.main._await_post_merge_ci", new_callable=AsyncMock) as await_ci:
            resp = client.post("/internal/pr-merged", json={
                "pr_url": "http://forgejo/owner/repo/pulls/5",
                "pr_number": 5,
                "repo_full_name": "owner/repo",
            })
        assert resp.status_code == 200
        assert resp.json()["post_merge_ci"] == "pending"
        await_ci.assert_called_once()

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
             patch("event_bus.main.update_state", return_value=changes), \
             patch("event_bus.main._post_pr_comment") as comment:
            resp = client.post("/internal/recode-for-pr", json={
                "repo_full_name": "owner/repo",
                "pr_number": 5,
                "pr_url": "http://forgejo/owner/repo/pulls/5",
            })
        assert resp.status_code == 202
        assert resp.json()["status"] == "retry_cap_reached"
        comment.assert_called_once()  # operator is told auto-fix gave up

    def test_flag_recode_stuck_comments_and_parks(self, monkeypatch):
        from event_bus import main as m
        states = []
        monkeypatch.setattr(m, "update_state", lambda i, s: states.append(s))
        with patch("event_bus.main._post_pr_comment") as comment:
            m._flag_recode_stuck("owner/repo", 5, "story-1", "no_changes", 1)
        comment.assert_called_once()
        body = comment.call_args[0][2]
        assert "human attention" in body.lower()
        assert states == ["changes-requested"]

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

    def test_fetch_verdicts_falls_back_to_final_snapshot(self, monkeypatch):
        # When the live per-role keys are gone (gate cleared them), completed stories
        # still show R/T/S from the durable pr_verdict_final snapshot.
        import json
        r = fakeredis.FakeRedis()
        r.set("pr_verdict_final:owner:repo:1",
              json.dumps({"code_review": "pass", "test_run": "pass", "security": "warn"}))
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        from event_bus.main import _fetch_verdicts
        v = _fetch_verdicts("http://x/owner/repo/pulls/1")
        assert v == {"reviewer": "pass", "tester": "pass", "security": "warn"}

    def test_fetch_verdicts_live_key_wins_over_snapshot(self, monkeypatch):
        import json
        r = fakeredis.FakeRedis()
        r.set("pr_verdict:owner:repo:1:code_review", json.dumps({"status": "fail"}))  # live
        r.set("pr_verdict_final:owner:repo:1",
              json.dumps({"code_review": "pass", "test_run": "pass", "security": "pass"}))
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        from event_bus.main import _fetch_verdicts
        v = _fetch_verdicts("http://x/owner/repo/pulls/1")
        assert v["reviewer"] == "fail"          # live in-flight verdict wins
        assert v["tester"] == "pass" and v["security"] == "pass"  # snapshot fills the rest

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

    def test_reconcile_coder_slots_clears_leaked_count(self, monkeypatch):
        # A slot acquired before a crash/recreate is never released (its decrement
        # lived in the killed asyncio task). Startup reconciliation must reset it to 0.
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        from event_bus.main import _reconcile_coder_slots, _CODER_SLOT_KEY
        r.set(_CODER_SLOT_KEY, 2)  # leaked
        _reconcile_coder_slots()
        assert int(r.get(_CODER_SLOT_KEY)) == 0

    def test_reconcile_coder_slots_without_redis_is_noop(self, monkeypatch):
        monkeypatch.setattr("event_bus.main._redis_conn", None)
        from event_bus.main import _reconcile_coder_slots
        _reconcile_coder_slots()  # must not raise

    def test_recode_cap_per_stack(self):
        # Rust raises its cap; stacks without one fall back to the global default;
        # an unknown/empty stack resolves to generic (no cap) → global default.
        from event_bus.main import _recode_cap_for_item, _MAX_RECODE_RETRIES
        assert _recode_cap_for_item({"stack": "rust"}) == 5
        assert _recode_cap_for_item({"stack": "python"}) == _MAX_RECODE_RETRIES
        assert _recode_cap_for_item({"stack": ""}) == _MAX_RECODE_RETRIES

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

    def test_fresh_python_repo_scaffolds_stack(self, monkeypatch):
        from event_bus.ci_workflow import CI_WORKFLOW_PATH
        fj = MagicMock()
        fj.repo_exists.return_value = False
        m = self._patch_common(monkeypatch, fj)

        result = m._provision_project_repo("idea-1", "Add Login Page", "python")

        assert result == "devadmin/add-login-page"
        fj.create_repo.assert_called_once()
        # one batch commit: CI workflow + stack marker + python scaffold
        fj.create_files.assert_called_once()
        (owner, repo, files), kwargs = fj.create_files.call_args
        assert kwargs.get("branch") == "main"
        assert CI_WORKFLOW_PATH in files and "pytest" in files[CI_WORKFLOW_PATH]
        assert files[".devagents/stack"].strip() == "python"
        assert "pyproject.toml" in files
        # branch protection + bot collaborators unchanged
        fj.set_branch_protection.assert_called_once()
        bots = {c.args[2] for c in fj.add_collaborator.call_args_list}
        assert bots == {"coder-bot", "reviewer-bot"}
        fj.create_webhook.assert_called_once()

    def test_unknown_stack_falls_back_to_generic(self, monkeypatch):
        fj = MagicMock()
        fj.repo_exists.return_value = False
        m = self._patch_common(monkeypatch, fj)

        m._provision_project_repo("idea-x", "Some Project", "cobol")
        (_, _, files), _ = fj.create_files.call_args
        assert files[".devagents/stack"].strip() == "generic"

    def test_existing_repo_skips_scaffold(self, monkeypatch):
        fj = MagicMock()
        fj.repo_exists.return_value = True
        m = self._patch_common(monkeypatch, fj)

        result = m._provision_project_repo("idea-2", "Existing Project")

        assert result == "devadmin/existing-project"
        fj.create_repo.assert_not_called()
        fj.create_files.assert_not_called()
        fj.set_branch_protection.assert_not_called()
        fj.add_collaborator.assert_not_called()
        fj.create_webhook.assert_called_once()

    def test_scaffold_commit_failure_is_non_fatal(self, monkeypatch):
        fj = MagicMock()
        fj.repo_exists.return_value = False
        fj.create_files.side_effect = RuntimeError("contents API down")
        m = self._patch_common(monkeypatch, fj)

        # Failure to commit scaffold must not fail provisioning
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


# ── cost cap enforcement ──────────────────────────────────────────────────────

class TestCostCap:
    def test_submit_idea_blocked_when_over_budget(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main.over_budget", lambda *a, **k: True)
        resp = client.post("/api/ideas", json={"prompt": "build a thing"})
        assert resp.status_code == 429
        assert "cost cap" in resp.json()["detail"].lower()

    def test_submit_idea_allowed_when_under_budget(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main.over_budget", lambda *a, **k: False)
        fake = {"id": "i-1", "title": "T", "state": "pending-approval", "type": "idea"}
        with patch("idea_agent.main.expand_idea", return_value={"title": "T", "description": "D"}), \
             patch("event_bus.main.create_item", return_value=fake):
            resp = client.post("/api/ideas", json={"prompt": "build a thing"})
        assert resp.status_code == 202

    def test_handle_pr_event_cost_capped(self):
        from event_bus.jobs.handlers import handle_pr_event
        from event_bus.config_store import patch_config
        r = fakeredis.FakeRedis()
        patch_config(r, {"limits": {"max_cost_usd_daily": 0.01}})
        with patch("event_bus.cost_guard.today_spend", return_value=0.5), \
             patch("redis.from_url", return_value=r):
            result = handle_pr_event("owner/repo", 1, "abc123", "opened")
        assert result["status"] == "cost_capped"


# ── coding sandbox passes the coder-bot token ─────────────────────────────────

class TestCoderSandboxEnv:
    def test_coder_token_in_sandbox_env(self):
        from event_bus.main import _CODER_SANDBOX_ENV
        # The coding sandbox must forward FORGEJO_CODER_TOKEN so the coder runs as
        # the least-privilege coder-bot, not the admin token.
        assert "FORGEJO_CODER_TOKEN" in _CODER_SANDBOX_ENV
        assert "FORGEJO_API_TOKEN" in _CODER_SANDBOX_ENV


# ── EPIC 2: stack proposal + approval override ────────────────────────────────

class TestIdeaStackProposal:
    def test_idea_stores_validated_stack(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main.over_budget", lambda *a, **k: False)
        prop = {"title": "T", "description": "D", "proposed_stack": "go",
                "proposed_sdlc": "tdd", "stack_rationale": "fits"}
        with patch("idea_agent.main.expand_idea", return_value=prop):
            resp = client.post("/api/ideas", json={"prompt": "build a go service"})
        assert resp.status_code == 202
        body = resp.json()
        assert body["stack"] == "go" and body["sdlc"] == "tdd"

    def test_invalid_proposed_stack_falls_back(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main.over_budget", lambda *a, **k: False)
        prop = {"title": "T", "description": "D", "proposed_stack": "cobol",
                "proposed_sdlc": "waterfall"}
        with patch("idea_agent.main.expand_idea", return_value=prop):
            resp = client.post("/api/ideas", json={"prompt": "x"})
        body = resp.json()
        assert body["stack"] == "generic" and body["sdlc"] == "standard"


class TestApproveStackOverride:
    def _pending(self):
        from event_bus.work_store import create_item
        return create_item(item_type="idea", title="T", description="D",
                           state="pending-approval", stack="python", sdlc="standard")

    def test_override_applies(self, client):
        from event_bus.work_store import get_item
        it = self._pending()
        with patch("event_bus.main._run_planner", new_callable=AsyncMock):
            resp = client.post(f"/api/items/{it['id']}/approve", json={"stack": "go", "sdlc": "tdd"})
        assert resp.status_code == 200
        final = get_item(it["id"])
        assert final["stack"] == "go" and final["sdlc"] == "tdd" and final["state"] == "approved"

    def test_invalid_override_returns_422(self, client):
        it = self._pending()
        resp = client.post(f"/api/items/{it['id']}/approve", json={"stack": "cobol"})
        assert resp.status_code == 422

    def test_no_override_keeps_proposal(self, client):
        from event_bus.work_store import get_item
        it = self._pending()
        with patch("event_bus.main._run_planner", new_callable=AsyncMock):
            resp = client.post(f"/api/items/{it['id']}/approve", json={})
        assert resp.status_code == 200
        final = get_item(it["id"])
        assert final["stack"] == "python" and final["sdlc"] == "standard"


# ── EPIC 3: repo stack resolver ───────────────────────────────────────────────

class TestStackForRepo:
    def test_resolves_marker(self, monkeypatch):
        from event_bus import main as m
        import base64, coding_agent.forgejo_client as fc
        fj = MagicMock()
        fj.get.return_value = {"content": base64.b64encode(b"go\n").decode()}
        FakeClient = MagicMock()
        FakeClient.return_value.__enter__.return_value = fj
        monkeypatch.setattr(fc, "ForgejoClient", FakeClient)
        assert m._stack_id_for_repo("devadmin", "repo") == "go"

    def test_unknown_marker_falls_back(self, monkeypatch):
        from event_bus import main as m
        import base64, coding_agent.forgejo_client as fc
        fj = MagicMock()
        fj.get.return_value = {"content": base64.b64encode(b"cobol\n").decode()}
        FakeClient = MagicMock()
        FakeClient.return_value.__enter__.return_value = fj
        monkeypatch.setattr(fc, "ForgejoClient", FakeClient)
        assert m._stack_id_for_repo("devadmin", "repo") == "generic"

    def test_missing_marker_falls_back(self, monkeypatch):
        from event_bus import main as m
        import coding_agent.forgejo_client as fc
        fj = MagicMock()
        fj.get.side_effect = RuntimeError("404")
        FakeClient = MagicMock()
        FakeClient.return_value.__enter__.return_value = fj
        monkeypatch.setattr(fc, "ForgejoClient", FakeClient)
        assert m._stack_id_for_repo("devadmin", "repo") == "generic"


# ── EPIC 4: SDLC-aware planning wiring ────────────────────────────────────────

class TestPlannerSdlcWiring:
    def test_directive_passed_and_stories_tagged_in_order(self, monkeypatch):
        import asyncio
        from event_bus import main as m
        monkeypatch.setattr(m, "_redis_conn", fakeredis.FakeRedis())
        monkeypatch.setattr(m, "over_budget", lambda *a, **k: False)
        monkeypatch.setattr(m, "get_item", lambda _id: {
            "id": _id, "stack": "go", "sdlc": "tdd", "title": "T", "description": "D"})
        monkeypatch.setattr(m, "_provision_project_repo", lambda *a, **k: "devadmin/repo")
        monkeypatch.setattr(m, "set_repo", lambda *a, **k: None)
        monkeypatch.setattr(m, "update_state", lambda *a, **k: None)
        monkeypatch.setattr(m, "get_prompt", lambda *a, **k: "")
        monkeypatch.setattr(m, "_run_coding_agent", AsyncMock())

        created = []
        def fake_create_item(**kw):
            created.append(kw)
            return {"id": f"s{len(created)}", **kw}
        monkeypatch.setattr(m, "create_item", fake_create_item)

        plan = {"module_name": "M", "module_description": "d", "stories": [
            {"title": "write failing tests", "description": "x"},
            {"title": "implement feature", "description": "y"},
        ]}
        run_planner_mock = MagicMock(return_value=plan)
        monkeypatch.setattr("planner_agent.main.run_planner", run_planner_mock)

        asyncio.run(m._run_planner("idea-1", "T", "D"))

        kw = run_planner_mock.call_args.kwargs
        assert "fail" in kw["sdlc_directive"].lower()   # tdd directive
        assert kw["best_practices"]                      # go best practices non-empty
        # stories tagged with stack/sdlc and kept in planner order
        assert [c["sequence"] for c in created] == [1, 2]
        assert all(c["stack"] == "go" and c["sdlc"] == "tdd" for c in created)
        assert created[0]["title"] == "write failing tests"


# ── EPIC 5: stack-aware coder + reviewer ──────────────────────────────────────

class TestCoderStackContext:
    def test_augment_prepends_practices_and_directive(self):
        from event_bus.main import _augment_coder_prompt
        from event_bus.catalog import get_catalog
        c = get_catalog()
        out = _augment_coder_prompt("base prompt", c.get_stack("python"), c.get_sdlc("tdd"))
        assert "base prompt" in out
        assert "Stack conventions" in out and "PEP 8" in out
        assert "Development style" in out and "red" in out.lower()

    def test_augment_noop_for_generic_standard(self):
        from event_bus.main import _augment_coder_prompt
        from event_bus.catalog import get_catalog
        c = get_catalog()
        out = _augment_coder_prompt("base", c.get_stack("generic"), c.get_sdlc("standard"))
        assert out == "base"

    def test_coder_context_resolves(self, monkeypatch):
        from event_bus import main as m
        monkeypatch.setattr(m, "get_stack_sdlc_for_story", lambda _id: ("go", "tdd"))
        stack, sdlc = m._coder_context("s1")
        assert stack.id == "go" and sdlc.id == "tdd"

    def test_sandbox_falls_back_when_image_missing(self, monkeypatch):
        import sys, types
        fake_docker = types.ModuleType("docker")
        fake_errors = types.ModuleType("docker.errors")

        class ImageNotFound(Exception):
            pass
        fake_errors.ImageNotFound = ImageNotFound
        fake_docker.errors = fake_errors

        created = {"count": 0, "last": None}

        class FakeContainers:
            def create(self, image, **kw):
                created["count"] += 1
                created["last"] = image
                if image == "dev-agents/coder-go:latest":
                    raise ImageNotFound("nope")
                c = MagicMock()
                c.logs.return_value = [b"CODING_RESULT:" + json.dumps({"status": "ok"}).encode()]
                c.wait.return_value = {"StatusCode": 0}
                return c
        fake_client = MagicMock(); fake_client.containers = FakeContainers()
        fake_docker.from_env = lambda: fake_client
        monkeypatch.setitem(sys.modules, "docker", fake_docker)
        monkeypatch.setitem(sys.modules, "docker.errors", fake_errors)

        from event_bus import main as m
        monkeypatch.setattr(m.settings, "sandbox_image", "dev-agents/event-bus:latest")
        res = m._run_coding_agent_sandboxed_sync("s1", "T", "D", "p", None,
                                                 coder_image="dev-agents/coder-go:latest")
        assert res == {"status": "ok"}
        assert created["count"] == 2
        assert created["last"] == "dev-agents/event-bus:latest"


class TestReviewerStackPrompt:
    def test_reviewer_task_prompt_gets_stack_practices(self, monkeypatch):
        from event_bus.jobs.handlers import handle_pr_event
        r = fakeredis.FakeRedis()
        captured = {}
        class FakeQ:
            def __init__(self, *a, **k): pass
            def enqueue(self, func, *a, **k):
                if getattr(func, "__name__", "") == "run_code_reviewer":
                    captured.update(k)
                job = MagicMock(); job.id = "j"; return job
        with patch("redis.from_url", return_value=r), \
             patch("rq.Queue", FakeQ), \
             patch("event_bus.main._stack_id_for_repo", return_value="python"), \
             patch("event_bus.prompt_store.get_prompt", return_value="base task"):
            handle_pr_event("devadmin/repo", 1, "sha", "opened")
        assert "PEP 8" in captured.get("task_prompt", "")


# ── Post-merge CI gate: merged -> (CI) -> done | back-to-dev ───────────────────

class TestPostMergeCi:
    def test_ci_success_marks_done_and_advances(self, monkeypatch):
        import asyncio
        from event_bus import main as m
        states = {}
        monkeypatch.setattr(m, "update_state", lambda i, s: states.__setitem__(i, s))
        monkeypatch.setattr(m, "get_item", lambda i: {"id": i, "state": "merged"})
        monkeypatch.setattr(m, "_get_branch_head", lambda *a, **k: "sha123")
        monkeypatch.setattr(m, "_poll_commit_ci", lambda *a, **k: "success")
        advanced = []
        monkeypatch.setattr(m, "_advance_after_done", lambda i: advanced.append(i))
        asyncio.run(m._await_post_merge_ci("s-1", "dev/repo"))
        assert states["s-1"] == "done"
        assert advanced == ["s-1"]

    def test_ci_failure_returns_to_developer(self, monkeypatch):
        import asyncio
        from event_bus import main as m
        states = []
        monkeypatch.setattr(m, "update_state", lambda i, s: states.append(s))
        monkeypatch.setattr(m, "get_item",
                            lambda i: {"id": i, "state": "merged", "title": "T", "description": "d"})
        monkeypatch.setattr(m, "_get_branch_head", lambda *a, **k: "sha")
        monkeypatch.setattr(m, "_poll_commit_ci", lambda *a, **k: "failure")
        fixes = []
        monkeypatch.setattr(m, "_post_merge_fix", lambda i, res: fixes.append((i, res)))
        asyncio.run(m._await_post_merge_ci("s-1", "dev/repo"))
        assert "changes-requested" in states
        assert fixes == [("s-1", "failure")]

    def test_no_ci_workflow_still_marks_done(self, monkeypatch):
        import asyncio
        from event_bus import main as m
        states = {}
        monkeypatch.setattr(m, "update_state", lambda i, s: states.__setitem__(i, s))
        monkeypatch.setattr(m, "get_item", lambda i: {"id": i, "state": "merged"})
        monkeypatch.setattr(m, "_get_branch_head", lambda *a, **k: "sha")
        monkeypatch.setattr(m, "_poll_commit_ci", lambda *a, **k: "none")
        monkeypatch.setattr(m, "_advance_after_done", lambda i: None)
        asyncio.run(m._await_post_merge_ci("s-1", "dev/repo"))
        assert states["s-1"] == "done"

    def test_poll_commit_ci_reads_status(self, monkeypatch):
        from event_bus import main as m
        import coding_agent.forgejo_client as fc
        fj = MagicMock()
        fj.get.return_value = {"state": "success", "statuses": [{"status": "success"}]}
        FakeClient = MagicMock(); FakeClient.return_value.__enter__.return_value = fj
        monkeypatch.setattr(fc, "ForgejoClient", FakeClient)
        assert m._poll_commit_ci("dev/repo", "sha", timeout=5, interval=1, grace=1) == "success"

    def test_poll_commit_ci_failure(self, monkeypatch):
        from event_bus import main as m
        import coding_agent.forgejo_client as fc
        fj = MagicMock()
        fj.get.return_value = {"state": "failure", "statuses": [{"status": "failure"}]}
        FakeClient = MagicMock(); FakeClient.return_value.__enter__.return_value = fj
        monkeypatch.setattr(fc, "ForgejoClient", FakeClient)
        assert m._poll_commit_ci("dev/repo", "sha", timeout=5, interval=1, grace=1) == "failure"

    def test_post_merge_fix_respects_cap(self, monkeypatch):
        from event_bus import main as m
        r = fakeredis.FakeRedis()
        r.set("post_merge_fix:s-1", str(m._POST_MERGE_FIX_CAP))
        monkeypatch.setattr(m, "_redis_conn", r)
        monkeypatch.setattr(m, "get_item", lambda i: {"id": i, "title": "T", "description": "d"})
        dispatched = []
        monkeypatch.setattr(m, "_run_coding_agent", lambda *a, **k: dispatched.append(1))
        m._post_merge_fix("s-1", "failure")
        assert dispatched == []  # cap reached -> no further auto-fix, left for a human


# ── Style guides: proposal, approval override, injection ──────────────────────

class TestStyleGuidesApi:
    def test_list_style_guides_endpoint(self, client):
        resp = client.get("/api/style-guides")
        assert resp.status_code == 200
        ids = {g["id"] for g in resp.json()["style_guides"]}
        assert {"google-python", "human-voice"} <= ids
        assert all("applies_to_stacks" in g for g in resp.json()["style_guides"])

    def test_idea_persists_applicable_guides_only(self, client, monkeypatch):
        monkeypatch.setattr("event_bus.main.over_budget", lambda *a, **k: False)
        # propose a python-applicable guide + a go-only guide; only the applicable one sticks
        prop = {"title": "T", "description": "D", "proposed_stack": "python",
                "proposed_sdlc": "standard",
                "proposed_style_guides": ["google-python", "effective-go", "human-voice"]}
        with patch("idea_agent.main.expand_idea", return_value=prop):
            resp = client.post("/api/ideas", json={"prompt": "x"})
        body = resp.json()
        guides = (body.get("style_guides") or "").split(",")
        assert "google-python" in guides and "human-voice" in guides
        assert "effective-go" not in guides   # go-only, dropped for a python project

    def test_approve_accepts_style_guide_override(self, client):
        from event_bus.work_store import create_item, get_item
        it = create_item(item_type="idea", title="T", state="pending-approval",
                         stack="python", sdlc="standard", style_guides=["human-voice"])
        with patch("event_bus.main._run_planner", new_callable=AsyncMock):
            resp = client.post(f"/api/items/{it['id']}/approve",
                               json={"style_guides": ["google-python", "conventional-commits"]})
        assert resp.status_code == 200
        assert "google-python" in get_item(it["id"])["style_guides"]

    def test_approve_rejects_unknown_guide(self, client):
        from event_bus.work_store import create_item
        it = create_item(item_type="idea", title="T", state="pending-approval")
        resp = client.post(f"/api/items/{it['id']}/approve", json={"style_guides": ["bogus-guide"]})
        assert resp.status_code == 422

    def test_coder_prompt_includes_guides(self):
        from event_bus.main import _augment_coder_prompt
        from event_bus.catalog import get_catalog
        c = get_catalog()
        out = _augment_coder_prompt("base", c.get_stack("python"), c.get_sdlc("standard"),
                                    c.get_style_guides(["google-python"]))
        assert "Google Python" in out and "docstring" in out.lower()


class TestWatchdog:
    @staticmethod
    def _story(state, age_secs, **kw):
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(seconds=age_secs)).isoformat()
        return {"id": kw.get("id", "s1"), "type": "story", "state": state,
                "updated_at": ts, "pr_url": kw.get("pr_url"), "title": "S"}

    def test_is_story_stuck_thresholds(self):
        from event_bus.main import is_story_stuck
        assert is_story_stuck(self._story("in-progress", 1000)) is True   # >900s
        assert is_story_stuck(self._story("in-progress", 300)) is False
        assert is_story_stuck(self._story("in-review", 700, pr_url="http://x/pulls/1")) is True
        assert is_story_stuck(self._story("in-review", 700, pr_url=None)) is False  # no PR
        assert is_story_stuck(self._story("done", 99999)) is False        # terminal

    def test_sweep_recovers_stuck_in_progress(self, monkeypatch):
        import event_bus.main as m
        monkeypatch.setattr(m, "_redis_conn", fakeredis.FakeRedis())
        stuck = self._story("in-progress", 1000, id="sX")
        with patch.object(m, "list_items", return_value=[stuck]), \
             patch.object(m, "update_state") as upd, \
             patch.object(m, "_clear_recovery_keys"):
            m._sweep_stuck_stories()
        upd.assert_any_call("sX", "ready")     # reset the dead coder (chain re-dispatches)

    def test_requeue_open_pr_reenqueues_verdicts(self, monkeypatch):
        # Regression: this path used to raise NameError (undefined item_id) and never ran.
        import event_bus.main as m
        monkeypatch.setattr(m, "_redis_conn", fakeredis.FakeRedis())
        item = self._story("in-review", 700, id="sO", pr_url="http://f/o/r/pulls/7")
        fake_fj = MagicMock()
        fake_fj.__enter__.return_value.get.return_value = {"merged": False, "head": {"sha": "abc", "ref": "b"}, "base": {"ref": "main"}}
        q = MagicMock()
        with patch("coding_agent.forgejo_client.ForgejoClient", return_value=fake_fj), \
             patch.object(m, "_queue_or_503", return_value=q), \
             patch.object(m, "update_state") as upd, \
             patch.object(m, "_clear_recovery_keys"):
            assert m._requeue_pr_verdicts(item) is True
        q.enqueue.assert_called_once()          # verdicts re-run
        upd.assert_any_call("sO", "in-review")

    def test_requeue_merged_pr_advances_to_done(self, monkeypatch):
        # Lost post-merge advancement: PR already merged → advance, don't re-review.
        import event_bus.main as m
        monkeypatch.setattr(m, "_redis_conn", fakeredis.FakeRedis())
        item = self._story("in-review", 700, id="sM", pr_url="http://f/o/r/pulls/9")
        fake_fj = MagicMock()
        fake_fj.__enter__.return_value.get.return_value = {"merged": True, "head": {}, "base": {}}
        q = MagicMock()
        with patch("coding_agent.forgejo_client.ForgejoClient", return_value=fake_fj), \
             patch.object(m, "_queue_or_503", return_value=q), \
             patch.object(m, "update_state") as upd, \
             patch.object(m, "unlock_next_story") as unlock:
            assert m._requeue_pr_verdicts(item) is True
        upd.assert_any_call("sM", "done")       # advanced, not re-reviewed
        unlock.assert_called_once_with("sM")
        q.enqueue.assert_not_called()

    def test_sweep_recovers_stuck_in_review_via_verdict_requeue(self, monkeypatch):
        import event_bus.main as m
        monkeypatch.setattr(m, "_redis_conn", fakeredis.FakeRedis())
        stuck = self._story("in-review", 700, id="sR", pr_url="http://f/o/r/pulls/3")
        with patch.object(m, "list_items", return_value=[stuck]), \
             patch.object(m, "_requeue_pr_verdicts", return_value=True) as req:
            m._sweep_stuck_stories()
        req.assert_called_once()

    def test_sweep_gives_up_after_cap_and_flags_stuck(self, monkeypatch):
        import event_bus.main as m
        r = fakeredis.FakeRedis()
        monkeypatch.setattr(m, "_redis_conn", r)
        stuck = self._story("in-progress", 1000, id="sG")
        r.set("watch_attempts:sG", m._WATCH_ATTEMPT_CAP)   # already at cap
        with patch.object(m, "list_items", return_value=[stuck]), \
             patch.object(m, "update_state") as upd, \
             patch.object(m, "_dispatch_next_ready") as disp:
            m._sweep_stuck_stories()
        assert r.get("stuck:sG") == b"1"       # flagged for the operator
        upd.assert_not_called()                # did NOT re-dispatch
        disp.assert_not_called()

    def test_healthy_stories_untouched(self, monkeypatch):
        import event_bus.main as m
        monkeypatch.setattr(m, "_redis_conn", fakeredis.FakeRedis())
        fresh = self._story("in-progress", 60, id="ok")   # young, not stuck
        with patch.object(m, "list_items", return_value=[fresh]), \
             patch.object(m, "update_state") as upd:
            m._sweep_stuck_stories()
        upd.assert_not_called()


class TestAutoEscalate:
    _item = {"id": "s1", "state": "changes-requested", "type": "story", "title": "S",
             "pr_url": "http://f/o/r/pulls/9"}
    _payload = {"repo_full_name": "o/r", "pr_number": 9,
                "pr_url": "http://f/o/r/pulls/9", "feedback": "checks failed"}

    def _cfg(self, auto, esc=""):
        from event_bus.config_store import RuntimeConfig, GateConfig, ModelConfig
        return RuntimeConfig(gates=GateConfig(auto_escalate=auto), models=ModelConfig(escalate=esc))

    def test_escalates_at_cap_when_enabled(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        r.set("recode_retries:o/r:9", 3)          # already at cap
        r.set("stuck:s1", b"1")                   # a prior stuck flag should be cleared
        with patch("event_bus.main.find_item_by_pr_url", return_value=self._item), \
             patch("event_bus.main.get_config",
                   return_value=self._cfg(True, "openrouter/anthropic/claude-sonnet-4-6")), \
             patch("event_bus.main._recode_cap_for_item", return_value=3), \
             patch("event_bus.main._post_pr_comment") as comment, \
             patch("event_bus.main.update_state"), \
             patch("event_bus.main.get_prompt", return_value=""), \
             patch("coding_agent.main.fix_pr_review", return_value={"status": "error"}), \
             patch("coding_agent.forgejo_client.ForgejoClient"):
            client.post("/internal/recode-for-pr", json=self._payload)
        assert r.get("escalate_pr:o/r:9") == b"openrouter/anthropic/claude-sonnet-4-6"
        assert r.get("stuck:s1") is None          # actively retrying, not stuck
        assert any("escalat" in str(c).lower() for c in comment.call_args_list)

    def test_gives_up_when_already_escalated(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        r.set("recode_retries:o/r:9", 3)
        r.set("escalate_pr:o/r:9", b"openrouter/anthropic/claude-sonnet-4-6")  # already escalated
        with patch("event_bus.main.find_item_by_pr_url", return_value=self._item), \
             patch("event_bus.main.get_config", return_value=self._cfg(True)), \
             patch("event_bus.main._recode_cap_for_item", return_value=3), \
             patch("event_bus.main._post_pr_comment") as comment, \
             patch("event_bus.main.update_state"):
            resp = client.post("/internal/recode-for-pr", json=self._payload)
        d = resp.json()
        assert d["status"] == "retry_cap_reached" and d["escalated"] is True
        assert r.get("stuck:s1") == b"1"          # flagged for the attention tray
        assert any("gave up" in str(c).lower() for c in comment.call_args_list)

    def test_gives_up_immediately_when_disabled(self, client, monkeypatch):
        r = fakeredis.FakeRedis()
        monkeypatch.setattr("event_bus.main._redis_conn", r)
        r.set("recode_retries:o/r:9", 3)
        with patch("event_bus.main.find_item_by_pr_url", return_value=self._item), \
             patch("event_bus.main.get_config", return_value=self._cfg(False)), \
             patch("event_bus.main._recode_cap_for_item", return_value=3), \
             patch("event_bus.main._post_pr_comment"), \
             patch("event_bus.main.update_state"):
            resp = client.post("/internal/recode-for-pr", json=self._payload)
        assert resp.json()["escalated"] is False
        assert r.get("escalate_pr:o/r:9") is None    # no escalation when disabled

    def test_claude_code_allowed_on_reviewer_and_escalate(self, client, monkeypatch):
        # The reviewer verdict + a stalled story's escalation may use the subscription.
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        assert client.patch("/api/config",
                            json={"models": {"reviewer": "claude-code/sonnet"}}).status_code == 200
        assert client.patch("/api/config",
                            json={"models": {"escalate": "claude-code/opus"}}).status_code == 200

    def test_claude_code_rejected_on_coder(self, client, monkeypatch):
        # The opencode coder can't resolve claude-code models — reject to avoid a silent no-op.
        monkeypatch.setattr("event_bus.main._redis_conn", fakeredis.FakeRedis())
        resp = client.patch("/api/config", json={"models": {"coder": "claude-code/sonnet"}})
        assert resp.status_code == 422


class TestCreditExhaustionDetection:
    def test_detects_in_error_field(self):
        from event_bus.main import _looks_like_credit_exhaustion
        assert _looks_like_credit_exhaustion(
            {"status": "error", "error": "opencode rejected: requires more credits"}) is True

    def test_detects_in_reason_field(self):
        from event_bus.main import _looks_like_credit_exhaustion
        assert _looks_like_credit_exhaustion({"reason": "quota exceeded"}) is True

    def test_ignores_transient_errors(self):
        from event_bus.main import _looks_like_credit_exhaustion
        assert _looks_like_credit_exhaustion({"error": "connection reset by peer"}) is False


class TestRunningJobsByPr:
    """The board must show which PRs have a live agent job so a card doesn't look
    frozen while a verdict job is actually running (regression for the 'looks stuck
    but is reviewing' UX gap)."""

    def _job(self, func, repo, pr):
        j = MagicMock()
        j.func_name = func
        j.kwargs = {"repo_full_name": repo, "pr_number": pr}
        return j

    def _patch_rq(self, monkeypatch, running, queued):
        """Wire fake RQ registry/queue/redis into main for _running_jobs_by_pr."""
        import event_bus.main as m
        q = MagicMock()
        q.name = "agent-jobs"
        q.job_ids = [j.id for j in queued]
        monkeypatch.setattr(m, "_queue", q)
        monkeypatch.setattr(m, "_redis_conn", MagicMock())
        reg = MagicMock()
        reg.get_job_ids.return_value = [j.id for j in running]
        by_id = {j.id: j for j in (running + queued)}
        monkeypatch.setattr("rq.registry.StartedJobRegistry", lambda *a, **k: reg)
        monkeypatch.setattr("rq.job.Job.fetch", lambda jid, connection=None: by_id[jid])

    def test_running_job_maps_to_pr_with_label(self, monkeypatch):
        import event_bus.main as m
        run = [self._job("event_bus.jobs.pr_jobs.run_code_reviewer", "o/r", 32)]
        run[0].id = "j1"
        self._patch_rq(monkeypatch, running=run, queued=[])
        out = m._running_jobs_by_pr()
        assert out[("o/r", 32)] == {"label": "reviewing", "state": "running"}

    def test_running_wins_over_queued_for_same_pr(self, monkeypatch):
        import event_bus.main as m
        r = self._job("run_code_reviewer", "o/r", 7); r.id = "run"
        qd = self._job("run_tester", "o/r", 7); qd.id = "que"
        self._patch_rq(monkeypatch, running=[r], queued=[qd])
        out = m._running_jobs_by_pr()
        assert out[("o/r", 7)]["state"] == "running"

    def test_queued_only_is_labelled_queued(self, monkeypatch):
        import event_bus.main as m
        qd = self._job("run_security_scanner", "o/r", 5); qd.id = "q"
        self._patch_rq(monkeypatch, running=[], queued=[qd])
        out = m._running_jobs_by_pr()
        assert out[("o/r", 5)] == {"label": "security scan", "state": "queued"}

    def test_jobs_without_pr_kwargs_ignored(self, monkeypatch):
        import event_bus.main as m
        j = self._job("some_other_job", None, None); j.id = "x"; j.kwargs = {}
        self._patch_rq(monkeypatch, running=[j], queued=[])
        assert m._running_jobs_by_pr() == {}


class TestRecordCoderUsage:
    """Coder cost estimate: free model → $0, paid model → non-zero, always visible."""

    def _run(self, monkeypatch, model, meta):
        import event_bus.main as m
        import fakeredis, time
        r = fakeredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(m, "_redis_conn", r)
        monkeypatch.setattr("event_bus.models_catalog.get_model_meta", lambda *_a, **_k: meta)
        result = {"summary": "x" * 4000}  # ~1000 output tokens
        m._record_coder_usage(model, "y" * 400, "z" * 400, result, stack="python")  # ~200 in tok
        key = f"telemetry:llm:{time.strftime('%Y-%m-%d', time.gmtime())}"
        return r.hgetall(key), key

    def test_free_model_records_zero_cost_but_tokens_and_calls(self, monkeypatch):
        h, _ = self._run(monkeypatch, "openrouter/qwen/qwen3-coder:free",
                         {"price_prompt": 0.0, "price_completion": 0.0})
        pfx = "coder:openrouter/qwen/qwen3-coder:free"
        assert float(h[f"{pfx}:cost_usd"]) == 0.0
        assert int(h[f"{pfx}:calls"]) == 1
        assert int(h[f"{pfx}:output_tokens"]) == 1000     # 4000 chars / 4
        assert int(h[f"{pfx}:input_tokens"]) == 200       # 800 chars / 4

    def test_paid_model_records_nonzero_estimate(self, monkeypatch):
        h, _ = self._run(monkeypatch, "openrouter/minimax/minimax-m2.5",
                         {"price_prompt": 1.2e-07, "price_completion": 4.8e-07})
        pfx = "coder:openrouter/minimax/minimax-m2.5"
        # 200*1.2e-7 + 1000*4.8e-7 = 2.4e-5 + 4.8e-4
        assert abs(float(h[f"{pfx}:cost_usd"]) - (200*1.2e-7 + 1000*4.8e-7)) < 1e-9
        assert int(h[f"{pfx}:calls"]) == 1

    def test_missing_meta_is_safe_zero(self, monkeypatch):
        h, _ = self._run(monkeypatch, "openrouter/unknown/model", None)
        pfx = "coder:openrouter/unknown/model"
        assert float(h[f"{pfx}:cost_usd"]) == 0.0
        assert int(h[f"{pfx}:calls"]) == 1


class TestPostMergeCICancellation:
    def _fj(self, runs):
        from unittest.mock import MagicMock
        fj = MagicMock()
        fj.__enter__.return_value.get.return_value = {"workflow_runs": runs}
        return fj

    def test_cancelled_run_detected(self):
        import event_bus.main as m
        from unittest.mock import patch
        runs = [{"head_sha": "abc", "status": "cancelled"}, {"head_sha": "abc", "status": "cancelled"}]
        with patch("coding_agent.forgejo_client.ForgejoClient", return_value=self._fj(runs)):
            assert m._ci_cancelled("o/r", "abc") is True

    def test_genuine_failure_not_flagged_cancelled(self):
        import event_bus.main as m
        from unittest.mock import patch
        runs = [{"head_sha": "abc", "status": "failure"}, {"head_sha": "abc", "status": "cancelled"}]
        with patch("coding_agent.forgejo_client.ForgejoClient", return_value=self._fj(runs)):
            assert m._ci_cancelled("o/r", "abc") is False   # a real failure exists → not just cancelled

    def test_no_runs_for_sha_not_cancelled(self):
        import event_bus.main as m
        from unittest.mock import patch
        runs = [{"head_sha": "other", "status": "cancelled"}]
        with patch("coding_agent.forgejo_client.ForgejoClient", return_value=self._fj(runs)):
            assert m._ci_cancelled("o/r", "abc") is False
