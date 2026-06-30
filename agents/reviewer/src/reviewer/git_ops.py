"""Git operations for the reviewer agent."""

from __future__ import annotations
import re
import subprocess
from pathlib import Path


def clone(base_url: str, owner: str, repo: str, api_token: str, dest: str, branch: str = "") -> None:
    """Clone a Forgejo repo into dest using token-embedded URL.

    Clones full history (these repos are small) so the diff against the base branch
    can be computed reliably. A shallow, single-branch clone hides the merge-base, so
    `git diff base...head` came back empty and reviews ran on nothing.
    """
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"Unexpected base_url scheme: {base_url!r}")
    if not re.match(r"^[A-Za-z0-9_\-]+$", api_token):
        raise ValueError(f"Token contains unexpected characters")
    url = f"{base_url.rstrip('/')}/{owner}/{repo}.git"
    scheme, rest = url.split("://", 1)
    auth_url = f"{scheme}://devadmin:{api_token}@{rest}"
    cmd = ["git", "clone"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [auth_url, dest]
    subprocess.run(cmd, check=True, capture_output=True)


def checkout(repo_dir: str, sha: str) -> None:
    """Checkout a specific commit (detached HEAD). SHA must already be in the clone."""
    subprocess.run(
        ["git", "checkout", sha],
        cwd=repo_dir, check=True, capture_output=True,
    )


def get_diff(repo_dir: str, base_ref: str, head_sha: str) -> str:
    """Return the unified diff of head_sha against base_ref (changes the PR adds)."""
    # Ensure the base branch is present (full history → merge-base is reachable).
    subprocess.run(["git", "fetch", "origin", base_ref], cwd=repo_dir, capture_output=True)
    result = subprocess.run(
        ["git", "diff", f"origin/{base_ref}...{head_sha}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        return result.stdout
    # Fall back to a two-dot diff if the merge-base form yields nothing.
    two_dot = subprocess.run(
        ["git", "diff", f"origin/{base_ref}..{head_sha}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    return two_dot.stdout
