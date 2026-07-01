"""
End-to-end integration tests for the work-board pipeline orchestration.

These drive the REAL API endpoints + work-store state machine through the full
story lifecycle, mocking only the external boundaries — the LLM calls (idea
expansion, planner decomposition, the coding agent) and the git/CI side (Forgejo
provisioning, post-merge CI polling). The work board is the coordination backbone,
so this verifies the orchestration *composes* correctly across hops — the gap that
per-function unit tests don't cover.

The web process owns: idea -> approval -> planning -> coder dispatch -> merge
callback -> post-merge CI -> done -> next story. The reviewer/tester/security and
the merge decision run in the worker and report back via Redis verdicts and the
/internal/* endpoints; those seams are simulated here.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch, AsyncMock

import fakeredis
import pytest

from event_bus import main as m
from event_bus import work_store as ws


def _run(coro):
    """Run an orchestration coroutine and let its create_task() dispatches settle."""
    async def _wrap():
        await coro
        await asyncio.sleep(0)  # let scheduled _run_coding_agent dispatches record
    asyncio.run(_wrap())


@pytest.fixture
def redis(monkeypatch):
    r = fakeredis.FakeRedis()
    monkeypatch.setattr(m, "_redis_conn", r)
    monkeypatch.setattr(m, "over_budget", lambda *a, **k: False)
    return r


def _proposal(**kw):
    base = {"title": "Build a thing", "description": "Implement X.",
            "proposed_stack": "python", "proposed_sdlc": "standard",
            "proposed_style_guides": ["google-python", "human-voice"]}
    base.update(kw)
    return base


class TestPipelineLifecycle:
    def test_happy_path_idea_to_done_and_next_story(self, client, redis, monkeypatch):
        # ── 1. Submit idea (LLM expansion mocked) → pending-approval with proposal ──
        monkeypatch.setattr(m, "_redis_conn", redis)
        with patch("idea_agent.main.expand_idea", return_value=_proposal()):
            resp = client.post("/api/ideas", json={"prompt": "build a thing"})
        assert resp.status_code == 202
        idea = resp.json()
        idea_id = idea["id"]
        assert idea["state"] == "pending-approval"
        assert idea["stack"] == "python" and idea["sdlc"] == "standard"
        assert "google-python" in (idea["style_guides"] or "")

        # ── 2. Approve → planning. Planner LLM + provisioning mocked; assert the real
        #       work-store gets the stories, the first is dispatched, guides inherited ──
        plan = {"module_name": "M", "module_description": "d", "stories": [
            {"title": "Story one", "description": "do one"},
            {"title": "Story two", "description": "do two"},
        ]}
        # Disable the plan-approval gate here — this test targets the execution
        # lifecycle (auto-dispatch); the gate has its own unit tests.
        from event_bus.config_store import RuntimeConfig, GateConfig
        no_plan_gate = RuntimeConfig(gates=GateConfig(plan_approval=False))
        coder = AsyncMock()
        with patch("event_bus.main._provision_project_repo", return_value="dev/thing"), \
             patch("planner_agent.main.run_planner", return_value=plan), \
             patch("event_bus.main._run_coding_agent", coder), \
             patch("event_bus.main.get_config", return_value=no_plan_gate), \
             patch("event_bus.main.get_prompt", return_value=""):
            _run(m._run_planner(idea_id, idea["title"], idea["description"]))

        stories = sorted(
            [s for s in ws.list_items() if s.get("parent_id") == idea_id],
            key=lambda s: s.get("sequence") or 0,
        )
        assert [s["title"] for s in stories] == ["Story one", "Story two"]
        assert stories[0]["state"] == "in-progress"   # first story coding
        assert stories[1]["state"] == "backlog"        # rest wait
        assert stories[0]["stack"] == "python" and stories[0]["repo"] == "dev/thing"
        assert "google-python" in (stories[0]["style_guides"] or "")  # inherited from idea
        assert coder.call_args.args[0] == stories[0]["id"]  # coder dispatched for story 1

        s1, s2 = stories[0]["id"], stories[1]["id"]
        pr_url = "http://forge/dev/thing/pulls/1"

        # ── 3. Coder succeeds on story 1 (LLM/git boundary) → in-review + PR opened ──
        ws.update_state(s1, "in-review")
        ws.set_pr_url(s1, pr_url)
        assert ws.find_item_by_pr_url(pr_url)["id"] == s1

        # ── 4. Reviewer + tester + security green → the gate merges (worker seam) ──
        ws.update_state(s1, "merged")

        # ── 5. Post-merge CI on main passes → story done, next story unlocked+dispatched ─
        coder2 = AsyncMock()
        with patch("event_bus.main._get_branch_head", return_value="sha1"), \
             patch("event_bus.main._poll_commit_ci", return_value="success"), \
             patch("event_bus.main._run_coding_agent", coder2), \
             patch("event_bus.main.get_prompt", return_value=""):
            _run(m._await_post_merge_ci(s1, "dev/thing"))

        assert ws.get_item(s1)["state"] == "done"          # CI-verified terminal state
        assert ws.get_item(s2)["state"] == "in-progress"   # next story now coding
        assert coder2.call_args.args[0] == s2              # coder dispatched for story 2

    def test_post_merge_ci_failure_auto_fixes_then_parks_after_cap(self, redis, monkeypatch):
        idea = ws.create_item(item_type="idea", title="I", stack="python", sdlc="standard")
        s1 = ws.create_item(item_type="story", title="S1", parent_id=idea["id"],
                            sequence=1, state="merged", repo="dev/r")
        # First post-merge failure → an automatic fix pass is dispatched (re-codes).
        fix = AsyncMock()
        with patch("event_bus.main._get_branch_head", return_value="sha"), \
             patch("event_bus.main._poll_commit_ci", return_value="failure"), \
             patch("event_bus.main._run_coding_agent", fix), \
             patch("event_bus.main.get_prompt", return_value=""):
            _run(m._await_post_merge_ci(s1["id"], "dev/r"))
        assert ws.get_item(s1["id"])["state"] == "in-progress"   # auto-fix in flight
        assert fix.call_args.args[0] == s1["id"]

        # Once the fix cap is exhausted, it's parked in changes-requested for a human.
        redis.set(f"post_merge_fix:{s1['id']}", str(m._POST_MERGE_FIX_CAP))
        ws.update_state(s1["id"], "merged")
        capped = AsyncMock()
        with patch("event_bus.main._get_branch_head", return_value="sha"), \
             patch("event_bus.main._poll_commit_ci", return_value="failure"), \
             patch("event_bus.main._run_coding_agent", capped), \
             patch("event_bus.main.get_prompt", return_value=""):
            _run(m._await_post_merge_ci(s1["id"], "dev/r"))
        assert ws.get_item(s1["id"])["state"] == "changes-requested"  # left for a human
        capped.assert_not_called()
