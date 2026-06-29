"""Tests for git_ops using real temporary git repositories."""

import subprocess
from unittest.mock import patch
import pytest
from pathlib import Path

from coding_agent import git_ops


class TestCloneAuthUrl:
    def test_embeds_user_and_token(self):
        with patch("coding_agent.git_ops._run") as run:
            git_ops.clone("http://forgejo:3000", "devadmin", "repo", "abc123",
                          "/tmp/x", user="coder-bot")
        cmd = run.call_args[0][0]
        assert cmd[:2] == ["git", "clone"]
        assert cmd[2] == "http://coder-bot:abc123@forgejo:3000/devadmin/repo.git"

    def test_defaults_to_devadmin(self):
        with patch("coding_agent.git_ops._run") as run:
            git_ops.clone("http://forgejo:3000", "o", "r", "tok123", "/tmp/x")
        assert "devadmin:tok123@" in run.call_args[0][0][2]

    def test_rejects_bad_user(self):
        with pytest.raises(ValueError):
            git_ops.clone("http://f:3000", "o", "r", "tok", "/tmp/x", user="bad user!")


def _init_bare(tmp_path: Path) -> str:
    """Create a bare git repo and return its path."""
    bare = str(tmp_path / "bare.git")
    subprocess.run(["git", "init", "--bare", bare], check=True, capture_output=True)
    return bare


def _init_and_push(tmp_path: Path, bare: str) -> None:
    """Clone bare, add a file, and push so main exists."""
    work = str(tmp_path / "seed")
    subprocess.run(["git", "clone", bare, work], check=True, capture_output=True)
    (Path(work) / "README.md").write_text("# hello")
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@test.com"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=work, check=True, capture_output=True)
    subprocess.run(["git", "push", "origin", "HEAD:main"], cwd=work, check=True, capture_output=True)


class TestConfigureIdentity:
    def test_sets_name_and_email(self, tmp_path):
        bare = _init_bare(tmp_path)
        _init_and_push(tmp_path, bare)
        work = str(tmp_path / "clone")
        subprocess.run(["git", "clone", bare, work], check=True, capture_output=True)
        git_ops.configure_identity(work, "Agent", "agent@test.com")
        name = subprocess.run(["git", "config", "user.name"], cwd=work, capture_output=True, text=True).stdout.strip()
        assert name == "Agent"


class TestCreateBranch:
    def test_creates_branch(self, tmp_path):
        bare = _init_bare(tmp_path)
        _init_and_push(tmp_path, bare)
        work = str(tmp_path / "clone")
        subprocess.run(["git", "clone", bare, work], check=True, capture_output=True)
        git_ops.create_branch(work, "feature/test")
        current = subprocess.run(
            ["git", "branch", "--show-current"], cwd=work, capture_output=True, text=True
        ).stdout.strip()
        assert current == "feature/test"


class TestCommitAll:
    def test_commits_changes(self, tmp_path):
        bare = _init_bare(tmp_path)
        _init_and_push(tmp_path, bare)
        work = str(tmp_path / "clone")
        subprocess.run(["git", "clone", bare, work], check=True, capture_output=True)
        git_ops.configure_identity(work, "A", "a@b.com")
        (Path(work) / "new.txt").write_text("content")
        sha = git_ops.commit_all(work, "add file")
        assert len(sha) == 40

    def test_no_changes_returns_empty(self, tmp_path):
        bare = _init_bare(tmp_path)
        _init_and_push(tmp_path, bare)
        work = str(tmp_path / "clone")
        subprocess.run(["git", "clone", bare, work], check=True, capture_output=True)
        git_ops.configure_identity(work, "A", "a@b.com")
        sha = git_ops.commit_all(work, "nothing")
        assert sha == ""


class TestClone:
    def test_rejects_special_chars_in_token(self, tmp_path):
        with pytest.raises(ValueError, match="unexpected"):
            git_ops.clone("http://host", "owner", "repo", "bad!token", str(tmp_path / "out"))

    def test_rejects_bad_url_format(self, tmp_path):
        with pytest.raises(ValueError, match="Unexpected"):
            git_ops.clone("not-a-url", "owner", "repo", "goodtoken", str(tmp_path / "out"))
