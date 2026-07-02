"""Tests for the Claude Code subscription adapter (headless `claude -p`)."""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from planner_agent import claude_code
from planner_agent.claude_code import complete, is_claude_code_model, model_alias


def _proc(stdout: str, returncode: int = 0, stderr: str = ""):
    p = MagicMock()
    p.returncode = returncode
    p.stdout = stdout
    p.stderr = stderr
    return p


_OK = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": '{"ok": 1}', "usage": {"input_tokens": 10, "output_tokens": 5},
                  "duration_ms": 1200, "num_turns": 1})

_MSGS = [{"role": "system", "content": "You are a planner."},
         {"role": "user", "content": "Plan the thing."}]


class TestModelRouting:
    def test_is_claude_code_model(self):
        assert is_claude_code_model("claude-code")
        assert is_claude_code_model("claude-code/opus")
        assert not is_claude_code_model("openrouter/anthropic/claude-sonnet-4-6")
        assert not is_claude_code_model("")

    def test_model_alias(self):
        assert model_alias("claude-code") == "sonnet"        # default
        assert model_alias("claude-code/opus") == "opus"
        assert model_alias("claude-code/haiku") == "haiku"
        assert model_alias("claude-code/bogus") == "sonnet"  # unknown → safe default


class TestComplete:
    def test_success_returns_result_text(self):
        with patch("planner_agent.claude_code.subprocess.run", return_value=_proc(_OK)) as run:
            out = complete(_MSGS, model="claude-code/opus")
        assert out == '{"ok": 1}'
        cmd = run.call_args.args[0]
        assert cmd[1:] == ["-p", "--output-format", "json", "--model", "opus"]
        # prompt fed on stdin (system + user folded together), neutral cwd
        assert "You are a planner." in run.call_args.kwargs["input"]
        assert "Plan the thing." in run.call_args.kwargs["input"]
        assert run.call_args.kwargs["cwd"] == "/tmp"

    def test_nonzero_exit_raises(self):
        with patch("planner_agent.claude_code.subprocess.run",
                   return_value=_proc("", returncode=1, stderr="not logged in")):
            with pytest.raises(RuntimeError, match="exited 1"):
                complete(_MSGS, model="claude-code")

    def test_error_envelope_raises(self):
        bad = json.dumps({"type": "result", "subtype": "error_during_execution",
                          "is_error": True, "result": ""})
        with patch("planner_agent.claude_code.subprocess.run", return_value=_proc(bad)):
            with pytest.raises(RuntimeError, match="error result"):
                complete(_MSGS, model="claude-code")

    def test_missing_cli_raises_runtime_error(self):
        with patch("planner_agent.claude_code.subprocess.run",
                   side_effect=FileNotFoundError("claude")):
            with pytest.raises(RuntimeError, match="not found"):
                complete(_MSGS, model="claude-code")

    def test_timeout_raises_timeout_error(self):
        with patch("planner_agent.claude_code.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=90)):
            with pytest.raises(TimeoutError):
                complete(_MSGS, model="claude-code", timeout=90)

    def test_non_json_stdout_raises(self):
        with patch("planner_agent.claude_code.subprocess.run",
                   return_value=_proc("plain text, not an envelope")):
            with pytest.raises(RuntimeError, match="non-JSON"):
                complete(_MSGS, model="claude-code")
