"""Tests for the Forgejo API client (httpx mocked with respx)."""

import pytest
import respx
import httpx

from coding_agent.forgejo_client import ForgejoClient


BASE_FORGEJO = "http://forgejo.test"


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


def test_forgejo_create_file():
    import base64
    captured = {}

    def _handler(request):
        captured["body"] = request.content
        return httpx.Response(201, json={"content": {"path": ".forgejo/workflows/ci.yml"}})

    with respx.mock:
        respx.post(
            f"{BASE_FORGEJO}/api/v1/repos/alice/backend/contents/.forgejo/workflows/ci.yml"
        ).mock(side_effect=_handler)
        with ForgejoClient(BASE_FORGEJO, "token") as client:
            res = client.create_file(
                "alice", "backend", ".forgejo/workflows/ci.yml",
                "name: CI\n", "ci: add workflow", branch="main",
            )
    assert res["content"]["path"] == ".forgejo/workflows/ci.yml"
    # content is base64-encoded in the request body
    import json as _json
    sent = _json.loads(captured["body"])
    assert base64.b64decode(sent["content"]).decode() == "name: CI\n"
    assert sent["branch"] == "main"
    assert sent["message"] == "ci: add workflow"


def test_forgejo_set_branch_protection():
    captured = {}

    def _handler(request):
        captured["body"] = request.content
        return httpx.Response(201, json={"branch_name": "main"})

    with respx.mock:
        respx.post(
            f"{BASE_FORGEJO}/api/v1/repos/alice/backend/branch_protections"
        ).mock(side_effect=_handler)
        with ForgejoClient(BASE_FORGEJO, "token") as client:
            res = client.set_branch_protection("alice", "backend", "main")
    assert res["branch_name"] == "main"
    import json as _json
    sent = _json.loads(captured["body"])
    assert sent["branch_name"] == "main"
    assert sent["enable_push"] is False        # direct pushes blocked
    assert sent["required_approvals"] == 0      # no hard approval gate (admin auto-merge)


def test_forgejo_http_error_raises():
    with respx.mock:
        respx.get(f"{BASE_FORGEJO}/api/v1/repos/alice/nope").mock(
            return_value=httpx.Response(404)
        )
        with ForgejoClient(BASE_FORGEJO, "token") as client:
            with pytest.raises(httpx.HTTPStatusError):
                client.get_repo("alice", "nope")
