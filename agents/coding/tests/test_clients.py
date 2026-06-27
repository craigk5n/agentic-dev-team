"""Tests for Plane and Forgejo API clients (httpx mocked with respx)."""

import pytest
import respx
import httpx

from coding_agent.plane_client import PlaneClient
from coding_agent.forgejo_client import ForgejoClient


BASE_PLANE = "http://plane.test"
BASE_FORGEJO = "http://forgejo.test"


# ── PlaneClient ────────────────────────────────────────────────────────────────

def test_plane_get_issue():
    with respx.mock:
        respx.get(f"{BASE_PLANE}/api/v1/workspaces/ws/projects/proj/issues/issue1/").mock(
            return_value=httpx.Response(200, json={"id": "issue1", "name": "Story A"})
        )
        with PlaneClient(BASE_PLANE, "token", "ws") as client:
            issue = client.get_issue("proj", "issue1")
    assert issue["name"] == "Story A"


def test_plane_get_states():
    with respx.mock:
        respx.get(f"{BASE_PLANE}/api/v1/workspaces/ws/projects/proj/states/").mock(
            return_value=httpx.Response(200, json={"results": [{"id": "s1", "name": "Ready"}]})
        )
        with PlaneClient(BASE_PLANE, "token", "ws") as client:
            states = client.get_states("proj")
    assert states[0]["name"] == "Ready"


def test_plane_find_state_id_match():
    with respx.mock:
        respx.get(f"{BASE_PLANE}/api/v1/workspaces/ws/projects/proj/states/").mock(
            return_value=httpx.Response(200, json={"results": [
                {"id": "s1", "name": "Ready"},
                {"id": "s2", "name": "In Progress"},
            ]})
        )
        with PlaneClient(BASE_PLANE, "token", "ws") as client:
            sid = client.find_state_id("proj", "in progress")
    assert sid == "s2"


def test_plane_find_state_id_no_match():
    with respx.mock:
        respx.get(f"{BASE_PLANE}/api/v1/workspaces/ws/projects/proj/states/").mock(
            return_value=httpx.Response(200, json={"results": [{"id": "s1", "name": "Done"}]})
        )
        with PlaneClient(BASE_PLANE, "token", "ws") as client:
            sid = client.find_state_id("proj", "Missing")
    assert sid is None


def test_plane_transition_issue():
    with respx.mock:
        respx.patch(f"{BASE_PLANE}/api/v1/workspaces/ws/projects/proj/issues/i1/").mock(
            return_value=httpx.Response(200, json={"id": "i1", "state": "s2"})
        )
        with PlaneClient(BASE_PLANE, "token", "ws") as client:
            result = client.transition_issue("proj", "i1", "s2")
    assert result["state"] == "s2"


def test_plane_add_comment():
    with respx.mock:
        respx.post(f"{BASE_PLANE}/api/v1/workspaces/ws/projects/proj/issues/i1/comments/").mock(
            return_value=httpx.Response(201, json={"id": "c1"})
        )
        with PlaneClient(BASE_PLANE, "token", "ws") as client:
            result = client.add_comment("proj", "i1", "All done!")
    assert result["id"] == "c1"


def test_plane_http_error_raises():
    with respx.mock:
        respx.get(f"{BASE_PLANE}/api/v1/workspaces/ws/projects/proj/issues/bad/").mock(
            return_value=httpx.Response(404)
        )
        with PlaneClient(BASE_PLANE, "token", "ws") as client:
            with pytest.raises(httpx.HTTPStatusError):
                client.get_issue("proj", "bad")


# ── ForgejoClient ─────────────────────────────────────────────────────────────

def test_forgejo_get_repo():
    with respx.mock:
        respx.get(f"{BASE_FORGEJO}/api/v1/repos/alice/backend").mock(
            return_value=httpx.Response(200, json={"full_name": "alice/backend"})
        )
        with ForgejoClient(BASE_FORGEJO, "token") as client:
            repo = client.get_repo("alice", "backend")
    assert repo["full_name"] == "alice/backend"


def test_forgejo_create_branch():
    with respx.mock:
        respx.post(f"{BASE_FORGEJO}/api/v1/repos/alice/backend/branches").mock(
            return_value=httpx.Response(201, json={"name": "story-1/add-login"})
        )
        with ForgejoClient(BASE_FORGEJO, "token") as client:
            branch = client.create_branch("alice", "backend", "story-1/add-login")
    assert branch["name"] == "story-1/add-login"


def test_forgejo_create_pr():
    with respx.mock:
        respx.post(f"{BASE_FORGEJO}/api/v1/repos/alice/backend/pulls").mock(
            return_value=httpx.Response(201, json={
                "number": 7,
                "html_url": "http://forgejo.test/alice/backend/pulls/7",
            })
        )
        with ForgejoClient(BASE_FORGEJO, "token") as client:
            pr = client.create_pr("alice", "backend", "feat: login", "body", "story-1/add-login")
    assert pr["number"] == 7


def test_forgejo_get_pr():
    with respx.mock:
        respx.get(f"{BASE_FORGEJO}/api/v1/repos/alice/backend/pulls/7").mock(
            return_value=httpx.Response(200, json={"number": 7, "state": "open"})
        )
        with ForgejoClient(BASE_FORGEJO, "token") as client:
            pr = client.get_pr("alice", "backend", 7)
    assert pr["state"] == "open"


def test_forgejo_http_error_raises():
    with respx.mock:
        respx.get(f"{BASE_FORGEJO}/api/v1/repos/alice/nope").mock(
            return_value=httpx.Response(404)
        )
        with ForgejoClient(BASE_FORGEJO, "token") as client:
            with pytest.raises(httpx.HTTPStatusError):
                client.get_repo("alice", "nope")
