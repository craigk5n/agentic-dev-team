"""Forgejo API client for the Coding Agent."""

from __future__ import annotations
import httpx
import structlog
from typing import Any

log = structlog.get_logger()


class ForgejoClient:
    def __init__(self, base_url: str, api_token: str) -> None:
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(
            headers={"Authorization": f"token {api_token}", "Content-Type": "application/json"},
            timeout=30,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ForgejoClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        resp = self._http.get(f"{self._base}/api/v1/repos/{owner}/{repo}")
        resp.raise_for_status()
        return resp.json()

    def create_branch(self, owner: str, repo: str, branch: str, base: str = "main") -> dict[str, Any]:
        resp = self._http.post(
            f"{self._base}/api/v1/repos/{owner}/{repo}/branches",
            json={"new_branch_name": branch, "old_branch_name": base},
        )
        resp.raise_for_status()
        log.info("forgejo_branch_created", repo=f"{owner}/{repo}", branch=branch)
        return resp.json()

    def create_pr(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> dict[str, Any]:
        resp = self._http.post(
            f"{self._base}/api/v1/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": base},
        )
        resp.raise_for_status()
        pr = resp.json()
        log.info("forgejo_pr_created", repo=f"{owner}/{repo}", pr=pr.get("number"), url=pr.get("html_url"))
        return pr

    def post_pr_comment(self, owner: str, repo: str, pr_number: int, body: str) -> dict[str, Any]:
        resp = self._http.post(
            f"{self._base}/api/v1/repos/{owner}/{repo}/issues/{pr_number}/comments",
            json={"body": body},
        )
        resp.raise_for_status()
        return resp.json()

    def get_pr(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        resp = self._http.get(f"{self._base}/api/v1/repos/{owner}/{repo}/pulls/{pr_number}")
        resp.raise_for_status()
        return resp.json()

    def get(self, path: str) -> Any:
        """Generic GET against the Forgejo API v1 base."""
        resp = self._http.get(f"{self._base}/api/v1{path}")
        resp.raise_for_status()
        return resp.json()

    def create_repo(self, name: str, description: str = "", private: bool = False) -> dict[str, Any]:
        resp = self._http.post(
            f"{self._base}/api/v1/user/repos",
            json={"name": name, "description": description, "private": private,
                  "auto_init": True, "default_branch": "main"},
        )
        resp.raise_for_status()
        repo = resp.json()
        log.info("forgejo_repo_created", repo=repo.get("full_name"))
        return repo

    def create_file(self, owner: str, repo: str, path: str, content: str,
                    message: str, branch: str = "main") -> dict[str, Any]:
        """Create a new file on `branch` via the Forgejo contents API (base64 body)."""
        import base64
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        resp = self._http.post(
            f"{self._base}/api/v1/repos/{owner}/{repo}/contents/{path}",
            json={"content": b64, "message": message, "branch": branch},
        )
        resp.raise_for_status()
        log.info("forgejo_file_created", repo=f"{owner}/{repo}", path=path, branch=branch)
        return resp.json()

    def set_branch_protection(self, owner: str, repo: str, branch: str = "main",
                              enable_push: bool = False,
                              required_approvals: int = 0) -> dict[str, Any]:
        """
        Protect `branch`: block direct pushes so all changes go through PRs.

        Defaults intentionally do NOT require approvals or status checks: the
        reviewer agent auto-merges via the API as a repo admin, and Forgejo blocks
        even admin merges when a required approval/status check is unmet — so a hard
        gate here would deadlock the autonomous merge. CI still runs and reports a
        status; the event-bus verdict aggregation is the real merge gate.
        """
        resp = self._http.post(
            f"{self._base}/api/v1/repos/{owner}/{repo}/branch_protections",
            json={
                "branch_name": branch,
                "enable_push": enable_push,
                "required_approvals": required_approvals,
            },
        )
        resp.raise_for_status()
        log.info("forgejo_branch_protected", repo=f"{owner}/{repo}", branch=branch)
        return resp.json()

    def add_collaborator(self, owner: str, repo: str, username: str,
                         permission: str = "write") -> None:
        """Grant `username` collaborator access (permission: read|write|admin).
        Idempotent — Forgejo returns 204 whether adding or updating."""
        resp = self._http.put(
            f"{self._base}/api/v1/repos/{owner}/{repo}/collaborators/{username}",
            json={"permission": permission},
        )
        resp.raise_for_status()
        log.info("forgejo_collaborator_added", repo=f"{owner}/{repo}",
                 user=username, permission=permission)

    def repo_exists(self, owner: str, name: str) -> bool:
        resp = self._http.get(f"{self._base}/api/v1/repos/{owner}/{name}")
        return resp.status_code == 200

    def create_webhook(self, owner: str, repo: str, url: str, secret: str,
                       events: list[str] | None = None) -> dict[str, Any]:
        resp = self._http.post(
            f"{self._base}/api/v1/repos/{owner}/{repo}/hooks",
            json={
                "type": "gitea",
                "active": True,
                "events": events or ["push", "pull_request", "pull_request_review"],
                "config": {"url": url, "content_type": "json", "secret": secret},
            },
        )
        resp.raise_for_status()
        log.info("forgejo_webhook_created", repo=f"{owner}/{repo}", url=url)
        return resp.json()
