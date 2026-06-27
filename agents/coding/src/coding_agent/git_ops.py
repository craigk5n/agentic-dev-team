"""Git operations for the Coding Agent (subprocess-based)."""

from __future__ import annotations
import re
import subprocess
import os
from pathlib import Path
import structlog

log = structlog.get_logger()


def _run(cmd: list[str], cwd: str | None = None, env: dict | None = None) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def clone(
    clone_base_url: str,
    owner: str,
    repo: str,
    api_token: str,
    target_dir: str,
) -> str:
    """
    Clone repo into target_dir and return the working tree path.
    Embeds the API token in the URL for authentication.
    """
    # Sanitize token for embedding in URL — tokens are alphanum + underscores
    if not re.match(r'^[A-Za-z0-9_]+$', api_token):
        raise ValueError("API token contains unexpected characters")
    clone_url = clone_base_url.rstrip("/")
    # Insert credentials: http://user:token@host/owner/repo.git
    if "://" in clone_url:
        scheme, rest = clone_url.split("://", 1)
        auth_url = f"{scheme}://devadmin:{api_token}@{rest}/{owner}/{repo}.git"
    else:
        raise ValueError(f"Unexpected clone URL format: {clone_url!r}")

    _run(["git", "clone", auth_url, target_dir])
    log.info("git_cloned", repo=f"{owner}/{repo}", target=target_dir)
    return target_dir


def configure_identity(repo_dir: str, name: str, email: str) -> None:
    _run(["git", "config", "user.name", name], cwd=repo_dir)
    _run(["git", "config", "user.email", email], cwd=repo_dir)


def create_branch(repo_dir: str, branch_name: str) -> None:
    _run(["git", "checkout", "-b", branch_name], cwd=repo_dir)
    log.info("git_branch", branch=branch_name)


def checkout_branch(repo_dir: str, branch_name: str) -> None:
    """Switch to an existing remote branch."""
    _run(["git", "checkout", "-b", branch_name, f"origin/{branch_name}"], cwd=repo_dir)
    log.info("git_checkout", branch=branch_name)


def commit_all(repo_dir: str, message: str) -> str:
    _run(["git", "add", "-A"], cwd=repo_dir)
    result = subprocess.run(
        ["git", "diff", "--cached", "--stat"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if not result.stdout.strip():
        log.info("git_no_changes")
        return ""
    _run(["git", "commit", "-m", message], cwd=repo_dir)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    log.info("git_committed", sha=sha[:8], message=message[:60])
    return sha


def push(repo_dir: str, branch_name: str) -> None:
    _run(["git", "push", "origin", branch_name], cwd=repo_dir)
    log.info("git_pushed", branch=branch_name)
