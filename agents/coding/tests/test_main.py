"""Integration tests for the Coding Agent orchestrator (all I/O mocked)."""

import pytest
from unittest.mock import MagicMock, patch

from coding_agent.main import _branch_name, _extract_repo, run_coding_agent


def _make_plane(
    issue: dict | None = None,
    in_progress_id: str = "state-ip",
    in_review_id: str = "state-ir",
) -> MagicMock:
    plane = MagicMock()
    plane.__enter__ = lambda s: s
    plane.__exit__ = MagicMock(return_value=False)
    plane.get_issue.return_value = issue or {
        "id": "issue-1",
        "name": "Add login page",
        "sequence_id": 1,
        "description_stripped": "Implement a login form.",
    }
    plane.find_state_id.side_effect = lambda proj, name: (
        in_progress_id if "progress" in name.lower() else in_review_id
    )
    plane.transition_issue.return_value = {}
    plane.add_comment.return_value = {}
    return plane


def _make_forgejo(pr_url: str = "http://git/pulls/1") -> MagicMock:
    forgejo = MagicMock()
    forgejo.__enter__ = lambda s: s
    forgejo.__exit__ = MagicMock(return_value=False)
    forgejo.create_pr.return_value = {"number": 1, "html_url": pr_url}
    return forgejo


class TestRunCodingAgent:
    def test_success_flow(self, tmp_path):
        plane = _make_plane()
        forgejo = _make_forgejo()

        with (
            patch("coding_agent.main.PlaneClient", return_value=plane),
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone", return_value=str(tmp_path)),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value="a" * 40),
            patch("coding_agent.main.git_ops.push"),
            patch("coding_agent.main.run_agent", return_value="Implemented login form"),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__ = lambda s: str(tmp_path)
            mock_td.return_value.__exit__ = MagicMock(return_value=False)

            result = run_coding_agent("issue-1", "ws", "proj-1")

        assert result["status"] == "success"
        assert "pr_url" in result
        plane.transition_issue.assert_called()
        forgejo.create_pr.assert_called_once()
        plane.add_comment.assert_called()

    def test_no_changes_returns_no_changes(self, tmp_path):
        plane = _make_plane()
        forgejo = _make_forgejo()

        with (
            patch("coding_agent.main.PlaneClient", return_value=plane),
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone", return_value=str(tmp_path)),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value=""),  # no changes
            patch("coding_agent.main.git_ops.push"),
            patch("coding_agent.main.run_agent", return_value="done"),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__ = lambda s: str(tmp_path)
            mock_td.return_value.__exit__ = MagicMock(return_value=False)

            result = run_coding_agent("issue-1", "ws", "proj-1")

        assert result["status"] == "no_changes"
        forgejo.create_pr.assert_not_called()

    def test_clone_failure_comments_and_raises(self, tmp_path):
        plane = _make_plane()
        forgejo = _make_forgejo()

        with (
            patch("coding_agent.main.PlaneClient", return_value=plane),
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone", side_effect=RuntimeError("clone failed")),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__ = lambda s: str(tmp_path)
            mock_td.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(RuntimeError, match="clone failed"):
                run_coding_agent("issue-1", "ws", "proj-1")

        plane.add_comment.assert_called_once()
        assert "Clone failed" in plane.add_comment.call_args[0][2]

    def test_agent_failure_comments_and_raises(self, tmp_path):
        plane = _make_plane()
        forgejo = _make_forgejo()

        with (
            patch("coding_agent.main.PlaneClient", return_value=plane),
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone", return_value=str(tmp_path)),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.run_agent", side_effect=RuntimeError("bad key")),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__ = lambda s: str(tmp_path)
            mock_td.return_value.__exit__ = MagicMock(return_value=False)

            with pytest.raises(RuntimeError, match="bad key"):
                run_coding_agent("issue-1", "ws", "proj-1")

        plane.add_comment.assert_called()
        assert "Agent failed" in plane.add_comment.call_args[0][2]

    def test_uses_repo_from_description(self, tmp_path):
        plane = _make_plane(issue={
            "id": "issue-1",
            "name": "Thing",
            "sequence_id": 2,
            "description_stripped": "repo: alice/backend\nDo the thing.",
        })
        forgejo = _make_forgejo()

        with (
            patch("coding_agent.main.PlaneClient", return_value=plane),
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone", return_value=str(tmp_path)) as mock_clone,
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value="a" * 40),
            patch("coding_agent.main.git_ops.push"),
            patch("coding_agent.main.run_agent", return_value="done"),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__ = lambda s: str(tmp_path)
            mock_td.return_value.__exit__ = MagicMock(return_value=False)

            run_coding_agent("issue-1", "ws", "proj-1")

        # clone should be called with alice + backend
        call_kwargs = mock_clone.call_args
        assert call_kwargs[1]["owner"] == "alice"
        assert call_kwargs[1]["repo"] == "backend"
