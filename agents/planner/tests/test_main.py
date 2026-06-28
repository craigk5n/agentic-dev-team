"""Tests for the Planner Agent orchestrator and Plane client."""

import pytest
import respx
import httpx
from unittest.mock import MagicMock, patch

from planner_agent.main import run_planner


_PLAN = {
    "module_name": "Auth Module",
    "module_description": "All auth work",
    "stories": [
        {"title": "Hash passwords", "description": "Hash them.", "priority": "high"},
        {"title": "Issue JWTs", "description": "Issue tokens.", "priority": "medium"},
    ],
}


class TestRunPlanner:
    def test_returns_plan_from_decomposer(self):
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN):
            result = run_planner("idea-1", "Auth System", "Build auth.")
        assert result == _PLAN

    def test_passes_title_and_description_to_decomposer(self):
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN) as mock:
            run_planner("idea-1", "Auth System", "Build the auth system.")
        args = mock.call_args.args
        assert args[0] == "Auth System"
        assert args[1] == "Build the auth system."

    def test_model_override_passed_to_decomposer(self):
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN) as mock:
            run_planner("idea-1", "T", "D", model_override="gpt-4o")
        assert mock.call_args.kwargs["model"] == "gpt-4o"

    def test_uses_settings_model_when_no_override(self):
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN) as mock, \
             patch("planner_agent.main.settings") as mock_settings:
            mock_settings.model_planner = "settings-model"
            mock_settings.effective_api_key = "key"
            mock_settings.default_repo = "dev/sandbox"
            run_planner("idea-1", "T", "D")
        assert mock.call_args.kwargs["model"] == "settings-model"

    def test_repo_full_name_passed_as_default_repo(self):
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN) as mock:
            run_planner("idea-1", "T", "D", repo_full_name="alice/backend")
        assert mock.call_args.kwargs["default_repo"] == "alice/backend"


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
