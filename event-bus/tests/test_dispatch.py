"""Integration-level tests for dispatch routing and the HTTP endpoints."""

import json
from tests.conftest import (
    FORGEJO_PR_OPENED,
    PLANE_IDEA_APPROVED,
    PLANE_ISSUE_READY,
    sign_forgejo,
    sign_plane,
)
from event_bus.dispatch import DispatchResult


class TestPlaneDispatch:
    def test_story_ready_enqueues_code_job(self, client, mock_queue):
        body = json.dumps(PLANE_ISSUE_READY).encode()
        sig = sign_plane(body)
        resp = client.post(
            "/webhook/plane",
            content=body,
            headers={"content-type": "application/json", "x-plane-signature": sig},
        )
        assert resp.status_code == 202
        data = resp.json()
        assert data["result"] == DispatchResult.ENQUEUED
        assert data["job_id"] == "test-job-id-001"
        mock_queue.enqueue.assert_called_once()

    def test_idea_approved_enqueues_plan_job(self, client, mock_queue):
        body = json.dumps(PLANE_IDEA_APPROVED).encode()
        sig = sign_plane(body)
        resp = client.post(
            "/webhook/plane",
            content=body,
            headers={"content-type": "application/json", "x-plane-signature": sig},
        )
        assert resp.status_code == 202
        assert resp.json()["result"] == DispatchResult.ENQUEUED

    def test_bad_signature_returns_403(self, client, mock_queue):
        body = json.dumps(PLANE_ISSUE_READY).encode()
        resp = client.post(
            "/webhook/plane",
            content=body,
            headers={"content-type": "application/json", "x-plane-signature": "bad"},
        )
        assert resp.status_code == 403
        mock_queue.enqueue.assert_not_called()

    def test_non_issue_event_is_skipped(self, client, mock_queue):
        payload = {**PLANE_ISSUE_READY, "event": "cycle"}
        body = json.dumps(payload).encode()
        sig = sign_plane(body)
        resp = client.post(
            "/webhook/plane",
            content=body,
            headers={"content-type": "application/json", "x-plane-signature": sig},
        )
        assert resp.status_code == 202
        assert resp.json()["result"] == DispatchResult.SKIPPED
        mock_queue.enqueue.assert_not_called()

    def test_delete_action_is_skipped(self, client, mock_queue):
        payload = {**PLANE_ISSUE_READY, "action": "deleted"}
        body = json.dumps(payload).encode()
        sig = sign_plane(body)
        resp = client.post(
            "/webhook/plane",
            content=body,
            headers={"content-type": "application/json", "x-plane-signature": sig},
        )
        assert resp.status_code == 202
        assert resp.json()["result"] == DispatchResult.SKIPPED

    def test_unmapped_state_is_skipped(self, client, mock_queue):
        payload = {
            **PLANE_ISSUE_READY,
            "payload": {
                "id": "x",
                "name": "x",
                "state_detail": {"id": "s", "name": "Weird State", "group": "started"},
            },
        }
        body = json.dumps(payload).encode()
        sig = sign_plane(body)
        resp = client.post(
            "/webhook/plane",
            content=body,
            headers={"content-type": "application/json", "x-plane-signature": sig},
        )
        assert resp.status_code == 202
        assert resp.json()["result"] == DispatchResult.SKIPPED


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
