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

    def _run_with_summary(self, tmp_path, summary):
        forgejo = _make_forgejo()
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value=""),  # no changes
            patch("coding_agent.main.run_opencode_agent", return_value=summary),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            result = run_coding_agent(_UUID, "Add login page", "Implement login form.")
        return result, forgejo

    def test_genuine_no_changes_skips_pr(self, tmp_path):
        # A substantive summary + no diff == the coder really examined the code and found
        # nothing to do → no_changes (done).
        summary = ("Reviewed the auth module; the login page and form already exist and "
                   "match the spec, so no code changes were necessary.")
        result, forgejo = self._run_with_summary(tmp_path, summary)
        assert result["status"] == "no_changes"
        forgejo.create_pr.assert_not_called()

    def test_empty_run_is_error_not_no_changes(self, tmp_path):
        # An output-less run with no diff means the coder never did anything (e.g. a model
        # error) — must be an error (retryable), NOT silently marked done.
        result, forgejo = self._run_with_summary(tmp_path, "Implementation complete")
        assert result["status"] == "error"
        forgejo.create_pr.assert_not_called()

    def test_tiny_summary_no_diff_is_error(self, tmp_path):
        result, _ = self._run_with_summary(tmp_path, "done")
        assert result["status"] == "error"

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
            patch("coding_agent.main.git_ops.merge_base_branch", return_value=True),
            patch("coding_agent.main.git_ops.has_unpushed", return_value=False),
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
            patch("coding_agent.main.git_ops.merge_base_branch", return_value=True),
            patch("coding_agent.main.git_ops.has_unpushed", return_value=False),
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

    def test_merge_advance_pushes_even_without_edits(self, tmp_path):
        # Clean merge brought the branch current but opencode made no edits ->
        # still push (the merge commit) and report success, not no_changes.
        forgejo = _make_forgejo()
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.checkout_branch"),
            patch("coding_agent.main.git_ops.merge_base_branch", return_value=True),
            patch("coding_agent.main.git_ops.has_unpushed", return_value=True),
            patch("coding_agent.main.git_ops.commit_all", return_value=""),
            patch("coding_agent.main.git_ops._run", return_value="c" * 40),
            patch("coding_agent.main.git_ops.push") as push,
            patch("coding_agent.main.run_opencode_agent", return_value="no edits"),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            result = fix_pr_review(_UUID, "t", "d", "branch", "alice/backend", [])
        assert result["status"] == "success"
        push.assert_called_once()


# ── In-coder TDD: test-and-iterate loop ───────────────────────────────────────

class TestRunTestCommand:
    def test_empty_command_skips(self):
        from coding_agent.main import _run_test_command
        assert _run_test_command("/tmp", "")[0] == "skipped"

    def test_missing_toolchain_skips(self):
        from coding_agent.main import _run_test_command
        status, out = _run_test_command("/tmp", "nonexistent-binary-xyz test ./...")
        assert status == "skipped"

    def test_passing_command(self, tmp_path):
        from coding_agent.main import _run_test_command
        assert _run_test_command(str(tmp_path), "true")[0] == "pass"

    def test_failing_command(self, tmp_path):
        from coding_agent.main import _run_test_command
        assert _run_test_command(str(tmp_path), "false")[0] == "fail"

    def test_missing_tooling_output_is_skipped(self, tmp_path):
        from coding_agent.main import _run_test_command
        cmd = "python3 -c \"import sys; sys.stderr.write('No module named pytest'); sys.exit(1)\""
        assert _run_test_command(str(tmp_path), cmd)[0] == "skipped"


class TestInCoderTdd:
    def _patches(self, tmp_path):
        return (
            patch("coding_agent.main.ForgejoClient", return_value=_make_forgejo()),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value="a" * 40),
            patch("coding_agent.main.git_ops.push"),
            patch("coding_agent.main.tempfile.TemporaryDirectory"),
        )

    def test_iterates_until_green(self, tmp_path):
        ps = self._patches(tmp_path)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6] as mock_td, \
             patch("coding_agent.main.run_opencode_agent", return_value="impl") as oc, \
             patch("coding_agent.main._run_test_command",
                   side_effect=[("fail", "boom"), ("pass", "ok")]) as rt:
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            result = run_coding_agent(_UUID, "T", "D", test_command="python3 -m pytest -q")
        assert result["test_status"] == "pass"
        assert rt.call_count == 2          # initial fail, then pass
        assert oc.call_count == 2          # initial write + one fix iteration

    def test_no_command_runs_no_tests(self, tmp_path):
        ps = self._patches(tmp_path)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6] as mock_td, \
             patch("coding_agent.main.run_opencode_agent", return_value="impl") as oc, \
             patch("coding_agent.main._run_test_command") as rt:
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            result = run_coding_agent(_UUID, "T", "D")  # no test_command
        rt.assert_not_called()
        assert result["test_status"] == "skipped"
        assert oc.call_count == 1

    def test_gives_up_after_max_iters(self, tmp_path):
        ps = self._patches(tmp_path)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6] as mock_td, \
             patch("coding_agent.main.run_opencode_agent", return_value="impl") as oc, \
             patch("coding_agent.main._run_test_command", return_value=("fail", "boom")):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            result = run_coding_agent(_UUID, "T", "D", test_command="pytest")
        # still opens the PR (CI/reviewer gate), reports failing
        assert result["status"] == "success"
        assert result["test_status"] == "fail"
        assert oc.call_count == 3          # initial + 2 retries (_MAX_TEST_ITERS)


class TestInstallBeforeTests:
    def test_run_install_empty_is_noop(self):
        from coding_agent.main import _run_install
        assert _run_install("/tmp", "") is None  # no command -> no-op, no raise

    def test_run_install_runs_command(self, tmp_path):
        from coding_agent.main import _run_install
        marker = tmp_path / "installed"
        _run_install(str(tmp_path), f"touch {marker}")
        assert marker.exists()

    def test_run_install_tolerates_failure(self, tmp_path):
        from coding_agent.main import _run_install
        # a failing install command must not raise
        assert _run_install(str(tmp_path), "exit 1") is None

    def test_install_runs_before_each_test_attempt(self, tmp_path):
        forgejo = _make_forgejo()
        with (
            patch("coding_agent.main.ForgejoClient", return_value=forgejo),
            patch("coding_agent.main.git_ops.clone"),
            patch("coding_agent.main.git_ops.configure_identity"),
            patch("coding_agent.main.git_ops.create_branch"),
            patch("coding_agent.main.git_ops.commit_all", return_value="a" * 40),
            patch("coding_agent.main.git_ops.push"),
            patch("coding_agent.main.tempfile.TemporaryDirectory") as mock_td,
            patch("coding_agent.main.run_opencode_agent", return_value="impl"),
            patch("coding_agent.main._run_install") as install,
            patch("coding_agent.main._run_test_command",
                  side_effect=[("fail", "x"), ("pass", "ok")]),
        ):
            mock_td.return_value.__enter__.return_value = str(tmp_path)
            mock_td.return_value.__exit__.return_value = False
            run_coding_agent(_UUID, "T", "D",
                             test_command="pytest", install_command="pip install -e .")
        assert install.call_count == 2  # once per test attempt (fail, then pass)
