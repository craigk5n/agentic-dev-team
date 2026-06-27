"""Shared fixtures for the event-bus test suite."""

from __future__ import annotations
import hashlib
import hmac
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from rq import Queue

import event_bus.main as _main_mod
from event_bus.config import settings


# ── helpers ───────────────────────────────────────────────────────────────────

def sign_plane(payload: bytes, secret: str = "test-secret") -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def sign_forgejo(payload: bytes, secret: str = "test-secret") -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def mock_queue() -> MagicMock:
    q = MagicMock(spec=Queue)
    job = MagicMock()
    job.id = "test-job-id-001"
    q.enqueue.return_value = job
    return q


@pytest.fixture()
def client(mock_queue, monkeypatch) -> TestClient:
    """TestClient with Redis mocked out so tests don't need a live Redis."""
    monkeypatch.setattr("event_bus.main._queue", mock_queue)
    monkeypatch.setattr("event_bus.main._redis_conn", MagicMock())
    monkeypatch.setattr(settings, "plane_webhook_secret", "test-secret")
    monkeypatch.setattr(settings, "forgejo_webhook_secret", "test-secret")
    monkeypatch.setattr(settings, "plane_workspace_slug", "dev-agents")
    return TestClient(_main_mod.app, raise_server_exceptions=True)


# ── sample payloads ───────────────────────────────────────────────────────────

PLANE_ISSUE_READY = {
    "event": "issue",
    "action": "updated",
    "workspace_id": "ws-001",
    "project_id": "proj-001",
    "payload": {
        "id": "issue-abc",
        "name": "Add login page",
        "state_detail": {
            "id": "state-001",
            "name": "Ready",
            "group": "started",
            "color": "#22c55e",
        },
    },
}

PLANE_IDEA_APPROVED = {
    "event": "issue",
    "action": "updated",
    "workspace_id": "ws-001",
    "project_id": "proj-001",
    "payload": {
        "id": "idea-xyz",
        "name": "New authentication system",
        "state_detail": {
            "id": "state-002",
            "name": "Approved",
            "group": "done",
            "color": "#16a34a",
        },
    },
}

FORGEJO_PR_OPENED = {
    "action": "opened",
    "number": 42,
    "pull_request": {
        "id": 101,
        "number": 42,
        "title": "feat: add login page",
        "state": "open",
        "merged": False,
        "head": {"ref": "feature/login", "sha": "abc123def456", "label": "feature/login"},
        "base": {"ref": "main", "sha": "000000000000", "label": "main"},
        "html_url": "http://localhost:13000/dev/myrepo/pulls/42",
    },
    "repository": {
        "id": 1,
        "name": "myrepo",
        "full_name": "dev/myrepo",
        "clone_url": "http://localhost:13000/dev/myrepo.git",
        "ssh_url": "git@localhost:dev/myrepo.git",
    },
    "sender": {"login": "devadmin"},
}
