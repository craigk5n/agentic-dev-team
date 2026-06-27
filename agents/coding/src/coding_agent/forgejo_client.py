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

    def get_pr(self, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
        resp = self._http.get(f"{self._base}/api/v1/repos/{owner}/{repo}/pulls/{pr_number}")
        resp.raise_for_status()
        return resp.json()

    def get(self, path: str) -> Any:
        """Generic GET against the Forgejo API v1 base."""
        resp = self._http.get(f"{self._base}/api/v1{path}")
        resp.raise_for_status()
        return resp.json()
