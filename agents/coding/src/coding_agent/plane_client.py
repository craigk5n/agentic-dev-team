"""Plane CE API client for the Coding Agent."""

from __future__ import annotations
import httpx
import structlog
from typing import Any

log = structlog.get_logger()


class PlaneClient:
    def __init__(self, base_url: str, api_token: str, workspace_slug: str) -> None:
        self._base = base_url.rstrip("/")
        self._slug = workspace_slug
        self._http = httpx.Client(
            headers={"x-api-key": api_token, "Content-Type": "application/json"},
            timeout=30,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PlaneClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def get_issue(self, project_id: str, issue_id: str) -> dict[str, Any]:
        resp = self._http.get(
            f"{self._base}/api/v1/workspaces/{self._slug}/projects/{project_id}/issues/{issue_id}/"
        )
        resp.raise_for_status()
        return resp.json()

    def get_states(self, project_id: str) -> list[dict[str, Any]]:
        resp = self._http.get(
            f"{self._base}/api/v1/workspaces/{self._slug}/projects/{project_id}/states/"
        )
        resp.raise_for_status()
        return resp.json().get("results", [])

    def find_state_id(self, project_id: str, state_name: str) -> str | None:
        for s in self.get_states(project_id):
            if s["name"].lower() == state_name.lower():
                return s["id"]
        return None

    def transition_issue(self, project_id: str, issue_id: str, state_id: str) -> dict[str, Any]:
        resp = self._http.patch(
            f"{self._base}/api/v1/workspaces/{self._slug}/projects/{project_id}/issues/{issue_id}/",
            json={"state": state_id},
        )
        resp.raise_for_status()
        log.info("plane_transition", issue=issue_id, state=state_id)
        return resp.json()

    def add_comment(self, project_id: str, issue_id: str, body: str) -> dict[str, Any]:
        resp = self._http.post(
            f"{self._base}/api/v1/workspaces/{self._slug}/projects/{project_id}/issues/{issue_id}/comments/",
            json={"comment_html": f"<p>{body}</p>"},
        )
        resp.raise_for_status()
        return resp.json()
