"""Tests for opencode_agent.py (subprocess mocked)."""

import subprocess
import pytest
from unittest.mock import MagicMock, patch

from coding_agent.opencode_agent import (
    run_opencode_agent, _clean, _looks_like_provider_error,
    _looks_like_model_not_found, ModelNotFoundError,
    _looks_like_credit_error, InsufficientCreditsError,
    parse_opencode_json_usage, extract_opencode_json_summary,
)


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

    def test_insufficient_credits_raises_distinct_error(self, tmp_path):
        # Out-of-credit is a distinct, unrecoverable error (not a generic provider error),
        # so the event bus can park the story instead of burning the retry cap.
        proc = _make_proc(["calling model…\n",
                           "OpenRouter: This request requires more credits, or fewer max_tokens\n"])
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc),
        ):
            with pytest.raises(InsufficientCreditsError):
                run_opencode_agent("Story", "desc", str(tmp_path), "model")


class TestCreditErrorDetection:
    def test_detects_credit_markers(self):
        assert _looks_like_credit_error("insufficient credits remaining") is True
        assert _looks_like_credit_error("quota exceeded for today") is True

    def test_ignores_generic_provider_errors(self):
        assert _looks_like_credit_error("temporarily rate-limited upstream") is False


class TestModelNotFound:
    _ERR = ["Calling model…\n",
            'ProviderModelNotFoundError: modelID: "anthropic/claude-sonnet-4-6"\n',
            "Error: Model not found: openrouter/anthropic/claude-sonnet-4-6\n"]

    def test_detection(self):
        assert _looks_like_model_not_found("ProviderModelNotFoundError: ...")
        assert _looks_like_model_not_found("Error: Model not found: x")
        assert not _looks_like_model_not_found("Added handling for a not-found 404 route")
        assert not _looks_like_model_not_found("Implementation complete")

    def test_raises_model_not_found_when_no_fallback(self, tmp_path):
        # opencode exits 0 but the model is unresolvable → distinct error (not "no changes").
        proc = _make_proc(self._ERR, returncode=0)
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc),
        ):
            with pytest.raises(ModelNotFoundError):
                run_opencode_agent("Story", "desc", str(tmp_path), "openrouter/anthropic/claude-sonnet-4-6")

    def test_falls_back_to_base_model(self, tmp_path):
        # First run (escalate model) is model-not-found; retry transparently on the
        # fallback model and return its output. Verify BOTH models were invoked.
        bad = _make_proc(self._ERR, returncode=0)
        good = _make_proc(["implemented the endpoint\n"], returncode=0)
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", side_effect=[bad, good]) as popen,
        ):
            result = run_opencode_agent(
                "Story", "desc", str(tmp_path),
                model="openrouter/anthropic/claude-sonnet-4-6",
                fallback_model="openrouter/minimax/minimax-m2.5")
        assert "implemented the endpoint" in result
        assert popen.call_count == 2
        models = [c.args[0][c.args[0].index("--model") + 1] for c in popen.call_args_list]
        assert models == ["openrouter/anthropic/claude-sonnet-4-6", "openrouter/minimax/minimax-m2.5"]

    def test_no_fallback_when_same_model(self, tmp_path):
        # If the fallback equals the failing model, don't retry (would just fail again).
        bad = _make_proc(self._ERR, returncode=0)
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", side_effect=[bad]) as popen,
        ):
            with pytest.raises(ModelNotFoundError):
                run_opencode_agent("Story", "desc", str(tmp_path), model="m", fallback_model="m")
        assert popen.call_count == 1


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


class TestParseOpencodeUsage:
    def test_parses_tokens_and_cost(self):
        from coding_agent.opencode_agent import parse_opencode_usage
        u = parse_opencode_usage("Done. 1,234 input tokens, 567 output tokens. Total cost: $0.0421")
        assert u == {"input_tokens": 1234, "output_tokens": 567, "cost_usd": 0.0421}

    def test_parses_prompt_completion_variants(self):
        from coding_agent.opencode_agent import parse_opencode_usage
        u = parse_opencode_usage("900 prompt tokens and 100 completion tokens used")
        assert u["input_tokens"] == 900 and u["output_tokens"] == 100

    def test_empty_when_no_usage(self):
        from coding_agent.opencode_agent import parse_opencode_usage
        assert parse_opencode_usage("Implementation complete") == {}
        assert parse_opencode_usage("") == {}


# ── Story 3.1: real opencode usage from --format json ──────────────────────────

def _amsg(mid, cost, i, o, r=0, model="anthropic/claude-sonnet-4-6"):
    import json
    return json.dumps({"type": "message.updated", "properties": {"info": {
        "id": mid, "role": "assistant", "modelID": model, "cost": cost,
        "tokens": {"input": i, "output": o, "reasoning": r,
                   "cache": {"read": 0, "write": 0}}}}})


def _text_part(pid, mid, text, synthetic=False):
    import json
    return json.dumps({"type": "message.part.updated", "properties": {"part": {
        "id": pid, "type": "text", "messageID": mid, "text": text,
        "synthetic": synthetic}}})


_JSON_STREAM = "\n".join([
    _amsg("m1", 0.01, 100, 50),                 # streaming update (superseded)
    _amsg("m1", 0.02, 200, 120, r=10),          # latest state for m1 wins
    _amsg("m2", 0.03, 80, 40),                   # second assistant turn
    '{"type":"message.updated","properties":{"info":{"id":"u1","role":"user"}}}',
    "not json at all",                           # tolerated
    _text_part("p1", "m1", "Implemented the widget."),
    _text_part("pu", "u1", "the original prompt"),   # user part — excluded
])


class TestParseOpencodeJsonUsage:
    def test_sums_deduped_across_assistant_messages(self):
        u = parse_opencode_json_usage(_JSON_STREAM)
        assert u["input_tokens"] == 280 and u["output_tokens"] == 160
        assert u["reasoning_tokens"] == 10
        assert u["cost_usd"] == pytest.approx(0.05)
        assert u["model"] == "anthropic/claude-sonnet-4-6"

    def test_empty_or_no_assistant_returns_empty(self):
        assert parse_opencode_json_usage("") == {}
        assert parse_opencode_json_usage("junk\nmore junk") == {}


class TestExtractOpencodeJsonSummary:
    def test_joins_assistant_text_parts_only(self):
        assert extract_opencode_json_summary(_JSON_STREAM) == "Implemented the widget."

    def test_empty_when_none(self):
        assert extract_opencode_json_summary("") == ""


class TestUsageOut:
    def test_json_mode_populates_real_usage_and_summary(self, tmp_path):
        proc = _make_proc([l + "\n" for l in _JSON_STREAM.splitlines()])
        usage: dict = {}
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc) as popen,
        ):
            summary = run_opencode_agent("S", "d", str(tmp_path), "model-x",
                                         usage_source="json", usage_out=usage)
        assert usage["source"] == "json"
        assert usage["input_tokens"] == 280 and usage["cost_usd"] == pytest.approx(0.05)
        assert usage["model_used"] == "model-x"
        assert summary == "Implemented the widget."
        assert "--format" in popen.call_args.args[0] and "json" in popen.call_args.args[0]

    def test_text_mode_records_model_used_but_no_source(self, tmp_path):
        proc = _make_proc(["did some work\n"])
        usage: dict = {}
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", return_value=proc) as popen,
        ):
            run_opencode_agent("S", "d", str(tmp_path), "model-x",
                               usage_source="text", usage_out=usage)
        assert usage["model_used"] == "model-x"
        assert "source" not in usage
        assert "--format" not in popen.call_args.args[0]

    def test_usage_out_records_fallback_model(self, tmp_path):
        # First model unresolved → falls back; usage_out must reflect the model that ran.
        proc_bad = _make_proc(["ProviderModelNotFoundError: no such model\n"])
        proc_ok = _make_proc(["ok\n"])
        usage: dict = {}
        with (
            patch("coding_agent.opencode_agent.shutil.which", return_value="/bin/opencode"),
            patch("coding_agent.opencode_agent.subprocess.Popen", side_effect=[proc_bad, proc_ok]),
        ):
            run_opencode_agent("S", "d", str(tmp_path), "bad-model",
                               fallback_model="good-model", usage_out=usage)
        assert usage["model_used"] == "good-model"
