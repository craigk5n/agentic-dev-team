"""Tests for opencode_agent.py (subprocess mocked)."""

import subprocess
import pytest
from unittest.mock import MagicMock, patch

from coding_agent.opencode_agent import run_opencode_agent, _clean, _looks_like_provider_error


class TestClean:
    def test_strips_ansi_escape(self):
        assert _clean("\x1b[31mred\x1b[0m") == "red"

    def test_strips_carriage_return(self):
        assert _clean("foo\r") == "foo"

    def test_plain_text_unchanged(self):
        assert _clean("hello world") == "hello world"

    def test_complex_ansi_sequence(self):
        assert _clean("\x1b[1;32mbold green\x1b[0m text") == "bold green text"


def _make_proc(lines=(), returncode=0, timeout=False):
    """Build a fake Popen-like mock."""
    mock_proc = MagicMock()
    mock_proc.stdout = list(lines)
    mock_proc.returncode = returncode
    if timeout:
        # First .wait() raises TimeoutExpired; second (after kill) returns None.
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired("opencode", 1), None]
    else:
        mock_proc.wait.return_value = None
    return mock_proc


class TestRunOpencodeAgent:
    def test_raises_when_opencode_not_found(self, tmp_path):
        with patch("coding_agent.opencode_agent.shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="opencode not found"):
                run_opencode_agent("Story", "desc", str(tmp_path), "model")

    def test_success_returns_output(self, tmp_path):
        proc = _make_proc(["line1\n", "line2\n"])
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc),
        ):
            result = run_opencode_agent("Story", "desc", str(tmp_path), "model")
        assert "line1" in result
        assert "line2" in result

    def test_empty_output_returns_default(self, tmp_path):
        proc = _make_proc()  # no output lines
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc),
        ):
            result = run_opencode_agent("Story", "desc", str(tmp_path), "model")
        assert result == "Implementation complete"

    def test_provider_error_output_raises_even_on_exit_0(self, tmp_path):
        # A 429 surfaced by opencode with a clean exit must raise, so the story is retried
        # rather than silently treated as "no changes".
        proc = _make_proc(["calling model…\n",
                           "qwen/qwen3-coder:free is temporarily rate-limited upstream\n"])
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc),
        ):
            with pytest.raises(RuntimeError, match="provider error/rate-limit"):
                run_opencode_agent("Story", "desc", str(tmp_path), "model")


class TestProviderErrorDetection:
    def test_flags_rate_limit_and_provider_errors(self):
        assert _looks_like_provider_error("... temporarily rate-limited upstream ...")
        assert _looks_like_provider_error("Provider returned error")
        assert _looks_like_provider_error("No endpoints found for model x")

    def test_does_not_flag_normal_code_output(self):
        assert not _looks_like_provider_error("Added error handling for 404 and 500 responses")
        assert not _looks_like_provider_error("Implementation complete")
        assert not _looks_like_provider_error("")

    def test_timeout_kills_process_and_raises(self, tmp_path):
        proc = _make_proc(timeout=True)
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc),
        ):
            with pytest.raises(RuntimeError, match="timed out"):
                run_opencode_agent("Story", "desc", str(tmp_path), "model", timeout=1)
        proc.kill.assert_called_once()

    def test_nonzero_exit_raises(self, tmp_path):
        proc = _make_proc(["error output\n"], returncode=1)
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc),
        ):
            with pytest.raises(RuntimeError, match="opencode exited 1"):
                run_opencode_agent("Story", "desc", str(tmp_path), "model")

    def test_with_review_comments_includes_feedback(self, tmp_path):
        proc = _make_proc(["fixed\n"])
        comments = [{"path": "main.py", "body": "Fix the indentation"}]
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            run_opencode_agent(
                "Story", "desc", str(tmp_path), "model", review_comments=comments
            )
        prompt = mock_popen.call_args.args[0][-1]
        assert "Fix the indentation" in prompt

    def test_review_comment_without_body_skipped(self, tmp_path):
        proc = _make_proc(["done\n"])
        comments = [{"path": "main.py", "body": ""}, {"path": "x.py", "body": "Real comment"}]
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            run_opencode_agent(
                "Story", "desc", str(tmp_path), "model", review_comments=comments
            )
        prompt = mock_popen.call_args.args[0][-1]
        assert "Real comment" in prompt

    def test_log_line_callback_called(self, tmp_path):
        proc = _make_proc(["line1\n", "line2\n"])
        captured = []
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc),
        ):
            run_opencode_agent(
                "Story", "desc", str(tmp_path), "model", log_line=captured.append
            )
        assert any("line1" in entry for entry in captured)

    def test_openrouter_api_key_set_in_env(self, tmp_path):
        proc = _make_proc(["ok\n"])
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            run_opencode_agent(
                "Story", "desc", str(tmp_path), "model", openrouter_api_key="mykey"
            )
        env = mock_popen.call_args.kwargs["env"]
        assert env["OPENROUTER_API_KEY"] == "mykey"

    def test_custom_prompt_template(self, tmp_path):
        proc = _make_proc(["done\n"])
        custom_template = "Custom: {story_title} - {story_description}"
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            run_opencode_agent(
                "MyStory", "my desc", str(tmp_path), "model",
                prompt_template=custom_template,
            )
        prompt = mock_popen.call_args.args[0][-1]
        assert prompt == "Custom: MyStory - my desc"

    def test_broken_template_falls_back_to_raw(self, tmp_path):
        """Template with unknown keys falls back to the raw template string."""
        proc = _make_proc(["done\n"])
        bad_template = "No valid {unknown_key} here"
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc) as mock_popen,
        ):
            run_opencode_agent(
                "Story", "desc", str(tmp_path), "model",
                prompt_template=bad_template,
            )
        prompt = mock_popen.call_args.args[0][-1]
        assert prompt == bad_template

    def test_log_line_exception_ignored(self, tmp_path):
        """Exceptions in the log_line callback should not crash the agent."""
        proc = _make_proc(["line\n"])

        def bad_callback(line):
            raise ValueError("callback exploded")

        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc),
        ):
            # Should not raise despite the bad callback
            result = run_opencode_agent(
                "Story", "desc", str(tmp_path), "model", log_line=bad_callback
            )
        assert "line" in result
