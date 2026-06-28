"""Tests for ForgejoClient and git_ops."""

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import httpx
import pytest
import respx

from reviewer.forgejo_client import ForgejoClient
from reviewer.git_ops import clone, checkout, get_diff

BASE = "http://forgejo.test"


# ── ForgejoClient ────────────────────────────────────────────────────────────

def test_post_pr_comment():
    with respx.mock:
        respx.post(f"{BASE}/api/v1/repos/alice/backend/issues/7/comments").mock(
            return_value=httpx.Response(201, json={"id": 99})
        )
        with ForgejoClient(BASE, "token") as client:
            result = client.post_pr_comment("alice", "backend", 7, "LGTM")
    assert result["id"] == 99


def test_get_pr():
    with respx.mock:
        respx.get(f"{BASE}/api/v1/repos/alice/backend/pulls/7").mock(
            return_value=httpx.Response(200, json={"number": 7, "state": "open"})
        )
        with ForgejoClient(BASE, "token") as client:
            pr = client.get_pr("alice", "backend", 7)
    assert pr["state"] == "open"


def test_create_review():
    with respx.mock:
        respx.post(f"{BASE}/api/v1/repos/alice/backend/pulls/7/reviews").mock(
            return_value=httpx.Response(200, json={"id": 1})
        )
        with ForgejoClient(BASE, "token") as client:
            result = client.create_review("alice", "backend", 7, "APPROVE", "Nice")
    assert result["id"] == 1


def test_http_error_raises():
    with respx.mock:
        respx.get(f"{BASE}/api/v1/repos/alice/nope/pulls/1").mock(
            return_value=httpx.Response(404)
        )
        with ForgejoClient(BASE, "token") as client:
            with pytest.raises(httpx.HTTPStatusError):
                client.get_pr("alice", "nope", 1)


# ── git_ops ────────────────────────────────────────────────────────────────────

def test_clone_rejects_bad_scheme():
    with pytest.raises(ValueError, match="Unexpected"):
        clone("ftp://host", "alice", "repo", "goodtoken", "/tmp/out")


def test_clone_rejects_bad_token():
    with pytest.raises(ValueError, match="unexpected"):
        clone("http://host", "alice", "repo", "bad!token", "/tmp/out")


def test_clone_calls_git(tmp_path):
    with patch("reviewer.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        clone("http://forgejo.test", "alice", "repo", "mytoken", str(tmp_path / "out"))
    cmd = mock_run.call_args[0][0]
    assert "git" in cmd and "clone" in cmd
    assert any("mytoken" in arg for arg in cmd)


def test_checkout_calls_git(tmp_path):
    with patch("reviewer.git_ops.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0)
        checkout(str(tmp_path), "abc123")
    calls = [c[0][0] for c in mock_run.call_args_list]
    # checkout() does a direct detached-HEAD checkout; SHA must already be in the clone.
    assert any("checkout" in c for c in calls)


def test_get_diff_returns_stdout(tmp_path):
    with patch("reviewer.git_ops.subprocess.run") as mock_run:
        fetch_result = MagicMock(returncode=0, stdout="")
        diff_result = MagicMock(returncode=0, stdout="+new line\n")
        mock_run.side_effect = [fetch_result, diff_result]
        diff = get_diff(str(tmp_path), "main", "abc123")
    assert "+new line" in diff
