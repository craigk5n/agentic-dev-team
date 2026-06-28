"""Tests for the Coding Agent orchestrator (all I/O mocked)."""

import pytest
from unittest.mock import MagicMock, patch

from coding_agent.main import run_coding_agent, fix_pr_review

_UUID = "abc12345-def0-0000-0000-000000000000"


def _make_forgejo(pr_url="http://git/org/repo/pulls/1"):
    forgejo = MagicMock()
    forgejo.__enter__ = lambda s: s
    forgejo.__exit__ = MagicMock(return_value=False)
    forgejo.create_pr.return_value = {"number": 1, "html_url": pr_url}
    forgejo.post_pr_comment.return_value = {}
    return forgejo


class TestRunCodingAgent:
    def test_success_flow(self, tmp_path):
        forgejo = _make_forgejo()
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value="a" * 40),
            patch("coding_agent.main.git_ops.push"),
            patch("coding_agent.main.run_opencode_agent", return_value="Implemented feature"),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            result = run_coding_agent(_UUID, "Add login page", "Implement login form.")
        assert result["status"] == "success"
        assert result["pr_url"] == "http://git/org/repo/pulls/1"
        assert result["sha"] == "a" * 40
        forgejo.create_pr.assert_called_once()

    def test_no_changes_skips_pr(self, tmp_path):
        forgejo = _make_forgejo()
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value=""),
            patch("coding_agent.main.run_opencode_agent", return_value="done"),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            result = run_coding_agent(_UUID, "Add login page", "Implement login form.")
        assert result["status"] == "no_changes"
        forgejo.create_pr.assert_not_called()

    def test_clone_failure_raises(self, tmp_path):
        forgejo = _make_forgejo()
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone", side_effect=RuntimeError("connection refused")),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            with pytest.raises(RuntimeError, match="Clone failed"):
                run_coding_agent(_UUID, "Add login page", "desc")

    def test_agent_failure_raises(self, tmp_path):
        forgejo = _make_forgejo()
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.run_opencode_agent", side_effect=RuntimeError("LLM error")),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            with pytest.raises(RuntimeError, match="LLM error"):
                run_coding_agent(_UUID, "Add login page", "desc")

    def test_uses_repo_from_description(self, tmp_path):
        forgejo = _make_forgejo()
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value="a" * 40),
            patch("coding_agent.main.git_ops.push"),
            patch("coding_agent.main.run_opencode_agent", return_value="done"),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            run_coding_agent(_UUID, "Thing", "repo: alice/backend\nDo the thing.")
        forgejo.create_pr.assert_called_once()
        assert forgejo.create_pr.call_args.kwargs["owner"] == "alice"
        assert forgejo.create_pr.call_args.kwargs["repo"] == "backend"

    def test_model_override_used(self, tmp_path):
        forgejo = _make_forgejo()
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value="a" * 40),
            patch("coding_agent.main.git_ops.push"),
            patch("coding_agent.main.run_opencode_agent", return_value="done") as mock_agent,
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            run_coding_agent(_UUID, "title", "desc", model_override="my-model")
        assert mock_agent.call_args.kwargs["model"] == "my-model"


class TestFixPrReview:
    def test_success_flow(self, tmp_path):
        forgejo = _make_forgejo()
        sha = "b" * 40
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.checkout_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value=sha),
            patch("coding_agent.main.git_ops.push"),
            patch("coding_agent.main.run_opencode_agent", return_value="Fixed issues"),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            result = fix_pr_review(
                _UUID, "Add login page", "desc",
                "story-abc12345/add-login-page", "alice/backend",
                [{"path": "main.py", "body": "Fix this"}],
            )
        assert result["status"] == "success"
        assert result["sha"] == sha
        assert result["item_id"] == _UUID

    def test_no_changes_returns_no_changes(self, tmp_path):
        forgejo = _make_forgejo()
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.checkout_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value=""),
            patch("coding_agent.main.run_opencode_agent", return_value="nothing to fix"),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            result = fix_pr_review(
                _UUID, "title", "desc", "branch", "alice/backend", []
            )
        assert result["status"] == "no_changes"
        assert result["item_id"] == _UUID
