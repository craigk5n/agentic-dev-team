"""Tests for the Planner Agent orchestrator and Plane client."""

import pytest
import respx
import httpx
from unittest.mock import MagicMock, patch


def _make_plane(ready_state_id="s-ready", module_id="mod-1"):
    plane = MagicMock()
    plane.__enter__ = lambda s: s
    plane.__exit__ = MagicMock(return_value=False)
    plane.get_issue.return_value = {
        "id": "idea-1",
        "name": "Auth System",
        "description_stripped": "Build auth.",
    }
    plane.find_state_id.return_value = ready_state_id
    plane.create_module.return_value = {"id": module_id, "name": "Auth Module"}
    plane.create_issue.side_effect = [
        {"id": "story-1", "name": "Hash passwords"},
        {"id": "story-2", "name": "Issue JWTs"},
    ]
    plane.add_to_module.return_value = {}
    plane.add_comment.return_value = {}
    return plane


_PLAN = {
    "module_name": "Auth Module",
    "module_description": "All auth work",
    "stories": [
        {"title": "Hash passwords", "description": "repo: dev/app\nHash them.", "priority": "high"},
        {"title": "Issue JWTs", "description": "repo: dev/app\nIssue tokens.", "priority": "medium"},
    ],
}


class TestRunPlanner:
    def test_creates_module_and_stories(self):
        plane = _make_plane()
        with (
            patch("planner_agent.main.PlaneClient", return_value=plane),
            patch("planner_agent.main.decompose_idea", return_value=_PLAN),
        ):
            from planner_agent.main import run_planner
            result = run_planner("idea-1", "ws", "proj-1")

        assert result["status"] == "planned"
        assert result["module_id"] == "mod-1"
        assert result["story_count"] == 2
        assert len(result["story_ids"]) == 2

    def test_all_stories_added_to_module(self):
        plane = _make_plane()
        with (
            patch("planner_agent.main.PlaneClient", return_value=plane),
            patch("planner_agent.main.decompose_idea", return_value=_PLAN),
        ):
            from planner_agent.main import run_planner
            run_planner("idea-1", "ws", "proj-1")

        assert plane.add_to_module.call_count == 2

    def test_comment_posted_on_idea(self):
        plane = _make_plane()
        with (
            patch("planner_agent.main.PlaneClient", return_value=plane),
            patch("planner_agent.main.decompose_idea", return_value=_PLAN),
        ):
            from planner_agent.main import run_planner
            run_planner("idea-1", "ws", "proj-1")

        plane.add_comment.assert_called_once()
        comment_body = plane.add_comment.call_args[0][2]
        assert "Planning complete" in comment_body
        assert "2" in comment_body  # story count

    def test_raises_when_no_ready_state(self):
        plane = _make_plane(ready_state_id=None)
        with (
            patch("planner_agent.main.PlaneClient", return_value=plane),
            patch("planner_agent.main.decompose_idea", return_value=_PLAN),
        ):
            from planner_agent.main import run_planner
            with pytest.raises(RuntimeError, match="Ready"):
                run_planner("idea-1", "ws", "proj-1")

    def test_stories_set_to_ready_state(self):
        plane = _make_plane(ready_state_id="s-ready-42")
        with (
            patch("planner_agent.main.PlaneClient", return_value=plane),
            patch("planner_agent.main.decompose_idea", return_value=_PLAN),
        ):
            from planner_agent.main import run_planner
            run_planner("idea-1", "ws", "proj-1")

        for call in plane.create_issue.call_args_list:
            assert call[1]["state_id"] == "s-ready-42"


class TestPlaneClient:
    def test_get_issue(self):
        with respx.mock:
            respx.get("http://plane.test/api/v1/workspaces/ws/projects/proj/issues/i1/").mock(
                return_value=httpx.Response(200, json={"id": "i1", "name": "Auth"})
            )
            from planner_agent.plane_client import PlaneClient
            with PlaneClient("http://plane.test", "token", "ws") as client:
                issue = client.get_issue("proj", "i1")
        assert issue["name"] == "Auth"

    def test_create_module(self):
        with respx.mock:
            respx.post("http://plane.test/api/v1/workspaces/ws/projects/proj/modules/").mock(
                return_value=httpx.Response(201, json={"id": "mod-1", "name": "Auth"})
            )
            from planner_agent.plane_client import PlaneClient
            with PlaneClient("http://plane.test", "token", "ws") as client:
                module = client.create_module("proj", "Auth", "Auth epic")
        assert module["id"] == "mod-1"

    def test_create_issue(self):
        with respx.mock:
            respx.post("http://plane.test/api/v1/workspaces/ws/projects/proj/issues/").mock(
                return_value=httpx.Response(201, json={"id": "i1"})
            )
            from planner_agent.plane_client import PlaneClient
            with PlaneClient("http://plane.test", "token", "ws") as client:
                issue = client.create_issue("proj", "title", "desc", "s1")
        assert issue["id"] == "i1"

    def test_add_to_module(self):
        with respx.mock:
            respx.post("http://plane.test/api/v1/workspaces/ws/projects/proj/modules/mod-1/module-issues/").mock(
                return_value=httpx.Response(201, json=[{"id": "i1"}])
            )
            from planner_agent.plane_client import PlaneClient
            with PlaneClient("http://plane.test", "token", "ws") as client:
                result = client.add_to_module("proj", "mod-1", "i1")
        assert result is not None

    def test_add_comment(self):
        with respx.mock:
            respx.post("http://plane.test/api/v1/workspaces/ws/projects/proj/issues/i1/comments/").mock(
                return_value=httpx.Response(201, json={"id": "c1"})
            )
            from planner_agent.plane_client import PlaneClient
            with PlaneClient("http://plane.test", "token", "ws") as client:
                result = client.add_comment("proj", "i1", "All done!")
        assert result["id"] == "c1"

    def test_find_state_id(self):
        with respx.mock:
            respx.get("http://plane.test/api/v1/workspaces/ws/projects/proj/states/").mock(
                return_value=httpx.Response(200, json={"results": [{"id": "s1", "name": "Ready"}]})
            )
            from planner_agent.plane_client import PlaneClient
            with PlaneClient("http://plane.test", "token", "ws") as client:
                sid = client.find_state_id("proj", "ready")
        assert sid == "s1"
