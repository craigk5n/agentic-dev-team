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
    user: str = "devadmin",
) -> str:
    """
    Clone repo into target_dir and return the working tree path.
    Embeds the token in the URL for authentication; `user` must own the token.
    """
    # Sanitize token for embedding in URL — tokens are alphanum + underscores
    if not re.match(r'^[A-Za-z0-9_]+$', api_token):
        raise ValueError("API token contains unexpected characters")
    if not re.match(r'^[A-Za-z0-9_-]+$', user):
        raise ValueError("git user contains unexpected characters")
    clone_url = clone_base_url.rstrip("/")
    # Insert credentials: http://user:token@host/owner/repo.git
    if "://" in clone_url:
        scheme, rest = clone_url.split("://", 1)
        auth_url = f"{scheme}://{user}:{api_token}@{rest}/{owner}/{repo}.git"
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


_PYTHON_GITIGNORE = """\
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
.venv/
*.egg
.env
"""

_NODE_GITIGNORE = """\
node_modules/
dist/
.env
*.log
"""

_GO_GITIGNORE = """\
*.exe
*.test
*.out
dist/
vendor/
.env
"""


def _ensure_gitignore(repo_dir: str) -> None:
    """Create a language-appropriate .gitignore if one doesn't exist."""
    root = Path(repo_dir)
    if (root / ".gitignore").exists():
        return
    # Detect language from files present
    has_py = any(root.rglob("*.py"))
    has_go = any(root.rglob("go.mod"))
    has_node = any(root.rglob("package.json"))
    if has_py:
        (root / ".gitignore").write_text(_PYTHON_GITIGNORE)
        log.info("gitignore_created", lang="python")
    elif has_go:
        (root / ".gitignore").write_text(_GO_GITIGNORE)
        log.info("gitignore_created", lang="go")
    elif has_node:
        (root / ".gitignore").write_text(_NODE_GITIGNORE)
        log.info("gitignore_created", lang="node")


def commit_all(repo_dir: str, message: str) -> str:
    _ensure_gitignore(repo_dir)
    # Un-track any artifact dirs that are now covered by .gitignore
    for pattern in ["__pycache__", "node_modules"]:
        subprocess.run(
            ["git", "rm", "-r", "--cached", "--ignore-unmatch", pattern],
            cwd=repo_dir, capture_output=True,
        )
    # Un-track egg-info dirs (name varies, find them)
    for p in Path(repo_dir).glob("*.egg-info"):
        subprocess.run(
            ["git", "rm", "-r", "--cached", "--ignore-unmatch", p.name],
            cwd=repo_dir, capture_output=True,
        )
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


def merge_base_branch(repo_dir: str, base: str = "main") -> bool:
    """Merge origin/<base> into the current branch to bring a stale PR up to date.

    Returns True if the merge was clean. On conflict, returns False and leaves the
    conflict markers in the working tree for the agent to resolve (the subsequent
    commit_all completes the merge).
    """
    _run(["git", "fetch", "origin", base], cwd=repo_dir)
    result = subprocess.run(
        ["git", "merge", "--no-edit", f"origin/{base}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    clean = result.returncode == 0
    log.info("git_merge_base", base=base, clean=clean)
    return clean


def has_unpushed(repo_dir: str, branch_name: str) -> bool:
    """True if the local branch has commits not yet on origin/<branch> (e.g. a merge)."""
    out = subprocess.run(
        ["git", "rev-list", "--count", f"origin/{branch_name}..HEAD"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    try:
        return int(out.stdout.strip() or "0") > 0
    except ValueError:
        return False
