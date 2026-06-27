"""Plane CE API client — scoped to idea creation."""

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

    def get_states(self, project_id: str) -> list[dict]:
        resp = self._client.get(f"/api/v1/workspaces/{self._ws}/projects/{project_id}/states/")
        resp.raise_for_status()
        return resp.json().get("results", [])

    def find_state_id(self, project_id: str, name: str) -> str | None:
        for s in self.get_states(project_id):
            if s["name"].lower() == name.lower():
                return s["id"]
        return None

    def get_labels(self, project_id: str) -> list[dict]:
        resp = self._client.get(f"/api/v1/workspaces/{self._ws}/projects/{project_id}/labels/")
        resp.raise_for_status()
        return resp.json().get("results", [])

    def create_label(self, project_id: str, name: str, color: str = "#6366f1") -> dict:
        resp = self._client.post(
            f"/api/v1/workspaces/{self._ws}/projects/{project_id}/labels/",
            json={"name": name, "color": color},
        )
        resp.raise_for_status()
        return resp.json()

    def find_or_create_label(self, project_id: str, name: str) -> str:
        """Return label ID for name, creating it if missing."""
        for label in self.get_labels(project_id):
            if label["name"].lower() == name.lower():
                return label["id"]
        return self.create_label(project_id, name)["id"]

    def create_issue(
        self,
        project_id: str,
        title: str,
        description_html: str,
        state_id: str,
        label_ids: list[str] | None = None,
    ) -> dict:
        resp = self._client.post(
            f"/api/v1/workspaces/{self._ws}/projects/{project_id}/issues/",
            json={
                "name": title,
                "description_html": description_html,
                "state": state_id,
                "label_ids": label_ids or [],
            },
        )
        resp.raise_for_status()
        return resp.json()
