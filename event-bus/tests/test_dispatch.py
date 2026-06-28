"""Integration-level tests for dispatch routing and the HTTP endpoints."""

import json
from tests.conftest import (
    FORGEJO_PR_OPENED,
    sign_forgejo,
)
from event_bus.dispatch import DispatchResult


class TestForgejoDispatch:
    def test_pr_opened_enqueues_review_job(self, client, mock_queue):
        body = json.dumps(FORGEJO_PR_OPENED).encode()
        sig = sign_forgejo(body)
        resp = client.post(
            "/webhook/forgejo",
            content=body,
            headers={
                "content-type": "application/json",
                "x-gitea-event": "pull_request",
                "x-gitea-signature": sig,
            },
        )
        assert resp.status_code == 202
        assert resp.json()["result"] == DispatchResult.ENQUEUED
        mock_queue.enqueue.assert_called_once()

    def test_bad_signature_returns_403(self, client, mock_queue):
        body = json.dumps(FORGEJO_PR_OPENED).encode()
        resp = client.post(
            "/webhook/forgejo",
            content=body,
            headers={
                "content-type": "application/json",
                "x-gitea-event": "pull_request",
                "x-gitea-signature": "wrong",
            },
        )
        assert resp.status_code == 403

    def test_non_pr_event_is_skipped(self, client, mock_queue):
        body = json.dumps({"action": "pushed"}).encode()
        sig = sign_forgejo(body)
        resp = client.post(
            "/webhook/forgejo",
            content=body,
            headers={
                "content-type": "application/json",
                "x-gitea-event": "push",
                "x-gitea-signature": sig,
            },
        )
        assert resp.status_code == 202
        assert resp.json()["result"] == "skipped"

    def test_pr_closed_action_is_skipped(self, client, mock_queue):
        payload = {**FORGEJO_PR_OPENED, "action": "closed"}
        body = json.dumps(payload).encode()
        sig = sign_forgejo(body)
        resp = client.post(
            "/webhook/forgejo",
            content=body,
            headers={
                "content-type": "application/json",
                "x-gitea-event": "pull_request",
                "x-gitea-signature": sig,
            },
        )
        assert resp.status_code == 202
        assert resp.json()["result"] == DispatchResult.SKIPPED


class TestHealth:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
