"""Plane CE API client — scoped to planner operations."""

from __future__ import annotations
import httpx


class PlaneClient:
    def __init__(self, base_url: str, token: str, workspace_slug: str) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"X-Api-Key": token, "Content-Type": "application/json"},
            timeout=30,
        )
        self._ws = workspace_slug

    def __enter__(self) -> "PlaneClient":
        return self

    def __exit__(self, *_) -> None:
        self._client.close()

    def get_issue(self, project_id: str, issue_id: str) -> dict:
        resp = self._client.get(
            f"/api/v1/workspaces/{self._ws}/projects/{project_id}/issues/{issue_id}/"
        )
        resp.raise_for_status()
        return resp.json()

    def get_states(self, project_id: str) -> list[dict]:
        resp = self._client.get(f"/api/v1/workspaces/{self._ws}/projects/{project_id}/states/")
        resp.raise_for_status()
        return resp.json().get("results", [])

    def find_state_id(self, project_id: str, name: str) -> str | None:
        for s in self.get_states(project_id):
            if s["name"].lower() == name.lower():
                return s["id"]
        return None

    def create_module(self, project_id: str, name: str, description: str) -> dict:
        resp = self._client.post(
            f"/api/v1/workspaces/{self._ws}/projects/{project_id}/modules/",
            json={"name": name, "description": description, "status": "In Progress"},
        )
        resp.raise_for_status()
        return resp.json()

    def create_issue(
        self,
        project_id: str,
        title: str,
        description: str,
        state_id: str,
        priority: str = "medium",
    ) -> dict:
        resp = self._client.post(
            f"/api/v1/workspaces/{self._ws}/projects/{project_id}/issues/",
            json={
                "name": title,
                "description_html": f"<p>{description}</p>",
                "state": state_id,
                "priority": priority,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def add_to_module(self, project_id: str, module_id: str, issue_id: str) -> dict:
        resp = self._client.post(
            f"/api/v1/workspaces/{self._ws}/projects/{project_id}/modules/{module_id}/module-issues/",
            json={"issues": [issue_id]},
        )
        resp.raise_for_status()
        return resp.json()

    def add_comment(self, project_id: str, issue_id: str, body: str) -> dict:
        resp = self._client.post(
            f"/api/v1/workspaces/{self._ws}/projects/{project_id}/issues/{issue_id}/comments/",
            json={"comment_html": f"<p>{body}</p>"},
        )
        resp.raise_for_status()
        return resp.json()
