"""Tests for the Idea Agent orchestrator."""

import pytest
from unittest.mock import MagicMock, patch


def _make_plane(state_id="s1", label_id="l1"):
    plane = MagicMock()
    plane.__enter__ = lambda s: s
    plane.__exit__ = MagicMock(return_value=False)
    plane.find_state_id.return_value = state_id
    plane.find_or_create_label.return_value = label_id
    plane.create_issue.return_value = {"id": "issue-1", "name": "JWT Auth"}
    return plane


class TestSubmitIdea:
    def test_success(self):
        proposal = {"title": "JWT Auth", "description": "## Overview\n\nBuild auth."}
        plane = _make_plane()

        with (
            patch("idea_agent.main.expand_prompt", return_value=proposal),
            patch("idea_agent.main.description_to_html", return_value="<h2>Overview</h2>"),
            patch("idea_agent.main.PlaneClient", return_value=plane),
        ):
            from idea_agent.main import submit_idea
            result = submit_idea("Add JWT auth", project_id="proj-1", workspace_slug="ws")

        assert result["status"] == "pending_approval"
        assert result["issue_id"] == "issue-1"
        assert result["title"] == "JWT Auth"
        assert "url" in result

    def test_raises_without_project_id(self):
        with patch("idea_agent.main.settings") as mock_settings:
            mock_settings.plane_project_id = ""
            mock_settings.plane_workspace_slug = "ws"
            from idea_agent.main import submit_idea
            with pytest.raises(ValueError, match="PLANE_PROJECT_ID"):
                submit_idea("prompt")

    def test_raises_when_no_pending_approval_state(self):
        proposal = {"title": "T", "description": "D"}
        plane = _make_plane(state_id=None)

        with (
            patch("idea_agent.main.expand_prompt", return_value=proposal),
            patch("idea_agent.main.description_to_html", return_value="<p>D</p>"),
            patch("idea_agent.main.PlaneClient", return_value=plane),
        ):
            from idea_agent.main import submit_idea
            with pytest.raises(RuntimeError, match="Pending Approval"):
                submit_idea("prompt", project_id="proj-1")

    def test_creates_idea_label(self):
        proposal = {"title": "T", "description": "D"}
        plane = _make_plane()

        with (
            patch("idea_agent.main.expand_prompt", return_value=proposal),
            patch("idea_agent.main.description_to_html", return_value=""),
            patch("idea_agent.main.PlaneClient", return_value=plane),
        ):
            from idea_agent.main import submit_idea
            submit_idea("prompt", project_id="proj-1")

        plane.find_or_create_label.assert_called_once_with("proj-1", "idea")

    def test_plane_client_receives_label(self):
        proposal = {"title": "T", "description": "D"}
        plane = _make_plane(label_id="label-99")

        with (
            patch("idea_agent.main.expand_prompt", return_value=proposal),
            patch("idea_agent.main.description_to_html", return_value=""),
            patch("idea_agent.main.PlaneClient", return_value=plane),
        ):
            from idea_agent.main import submit_idea
            submit_idea("prompt", project_id="proj-1")

        call_kwargs = plane.create_issue.call_args[1]
        assert "label-99" in call_kwargs["label_ids"]


class TestPlaneClient:
    def test_find_state_id_match(self):
        import respx, httpx
        with respx.mock:
            respx.get("http://plane.test/api/v1/workspaces/ws/projects/proj/states/").mock(
                return_value=httpx.Response(200, json={"results": [{"id": "s1", "name": "Pending Approval"}]})
            )
            from idea_agent.plane_client import PlaneClient
            with PlaneClient("http://plane.test", "token", "ws") as client:
                sid = client.find_state_id("proj", "pending approval")
        assert sid == "s1"

    def test_find_state_id_no_match(self):
        import respx, httpx
        with respx.mock:
            respx.get("http://plane.test/api/v1/workspaces/ws/projects/proj/states/").mock(
                return_value=httpx.Response(200, json={"results": []})
            )
            from idea_agent.plane_client import PlaneClient
            with PlaneClient("http://plane.test", "token", "ws") as client:
                sid = client.find_state_id("proj", "missing")
        assert sid is None

    def test_find_or_create_label_returns_existing(self):
        import respx, httpx
        with respx.mock:
            respx.get("http://plane.test/api/v1/workspaces/ws/projects/proj/labels/").mock(
                return_value=httpx.Response(200, json={"results": [{"id": "l1", "name": "idea"}]})
            )
            from idea_agent.plane_client import PlaneClient
            with PlaneClient("http://plane.test", "token", "ws") as client:
                lid = client.find_or_create_label("proj", "idea")
        assert lid == "l1"

    def test_create_issue(self):
        import respx, httpx
        with respx.mock:
            respx.post("http://plane.test/api/v1/workspaces/ws/projects/proj/issues/").mock(
                return_value=httpx.Response(201, json={"id": "i1", "name": "T"})
            )
            from idea_agent.plane_client import PlaneClient
            with PlaneClient("http://plane.test", "token", "ws") as client:
                issue = client.create_issue("proj", "T", "<p>desc</p>", "s1")
        assert issue["id"] == "i1"
