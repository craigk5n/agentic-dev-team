"""Forgejo API client — scoped to PR review operations."""

from __future__ import annotations
import httpx


class ForgejoClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
            timeout=30,
        )

    def __enter__(self) -> "ForgejoClient":
        return self

    def __exit__(self, *_) -> None:
        self._client.close()

    def post_pr_comment(self, owner: str, repo: str, pr_number: int, body: str) -> dict:
        resp = self._client.post(
            f"/api/v1/repos/{owner}/{repo}/issues/{pr_number}/comments",
            json={"body": body},
        )
        resp.raise_for_status()
        return resp.json()

    def get_pr(self, owner: str, repo: str, pr_number: int) -> dict:
        resp = self._client.get(f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}")
        resp.raise_for_status()
        return resp.json()

    def create_review(
        self, owner: str, repo: str, pr_number: int, event: str, body: str
    ) -> dict:
        """Create a PR review. event: 'COMMENT' | 'APPROVE' | 'REQUEST_CHANGES'"""
        resp = self._client.post(
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            json={"event": event, "body": body},
        )
        resp.raise_for_status()
        return resp.json()

    def merge_pr(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        method: str = "merge",
        message: str = "",
    ) -> dict:
        """
        Merge a PR via the Forgejo API.
        method: 'merge' | 'rebase' | 'squash' | 'fast-forward-only'
        Forgejo returns 204 No Content on success.
        """
        payload: dict = {"Do": method}
        if message:
            payload["merge_message_field"] = message
        resp = self._client.post(
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            json=payload,
        )
        resp.raise_for_status()
        return {"merged": True, "pr_number": pr_number}
