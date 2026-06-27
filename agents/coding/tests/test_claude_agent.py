"""Tests for the Claude agentic loop (mocked Anthropic client)."""

import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path

from coding_agent.claude_agent import (
    _execute_tool,
    _safe_path,
    run_agent,
    MAX_TURNS,
)


class TestExecuteTool:
    def test_read_file(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world")
        result = _execute_tool("read_file", {"path": "hello.txt"}, str(tmp_path))
        assert result == "hello world"

    def test_read_missing_file(self, tmp_path):
        result = _execute_tool("read_file", {"path": "nope.txt"}, str(tmp_path))
        assert "not found" in result

    def test_write_file(self, tmp_path):
        result = _execute_tool("write_file", {"path": "new.py", "content": "x=1"}, str(tmp_path))
        assert "Written" in result
        assert (tmp_path / "new.py").read_text() == "x=1"

    def test_write_creates_directories(self, tmp_path):
        _execute_tool("write_file", {"path": "a/b/c.py", "content": "pass"}, str(tmp_path))
        assert (tmp_path / "a" / "b" / "c.py").exists()

    def test_list_files(self, tmp_path):
        (tmp_path / "foo.py").write_text("")
        (tmp_path / "sub").mkdir()
        result = _execute_tool("list_files", {}, str(tmp_path))
        assert "foo.py" in result
        assert "sub" in result

    def test_list_hides_git_dir(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "src.py").write_text("")
        result = _execute_tool("list_files", {}, str(tmp_path))
        assert ".git" not in result

    def test_run_command_success(self, tmp_path):
        result = _execute_tool("run_command", {"command": "echo hello"}, str(tmp_path))
        assert "hello" in result
        assert "Exit 0" in result

    def test_run_command_failure(self, tmp_path):
        result = _execute_tool("run_command", {"command": "false"}, str(tmp_path))
        assert "Exit 1" in result

    def test_done_returns_sentinel(self, tmp_path):
        result = _execute_tool("done", {"summary": "all done"}, str(tmp_path))
        assert result == "__DONE__"

    def test_path_traversal_blocked(self, tmp_path):
        result = _execute_tool("read_file", {"path": "../secret"}, str(tmp_path))
        assert "Error" in result

    def test_unknown_tool_returns_error(self, tmp_path):
        result = _execute_tool("fly", {}, str(tmp_path))
        assert "Error" in result


class TestRunAgent:
    def test_rejects_invalid_api_key(self, tmp_path):
        with pytest.raises(RuntimeError, match="sk-ant-"):
            run_agent("title", "desc", str(tmp_path), api_key="not-real")

    def test_rejects_empty_api_key(self, tmp_path):
        with pytest.raises(RuntimeError, match="sk-ant-"):
            run_agent("title", "desc", str(tmp_path), api_key="")

    def test_done_tool_exits_loop(self, tmp_path):
        """Agent calls `done` in the first turn — should return immediately."""
        fake_tool_use = MagicMock()
        fake_tool_use.type = "tool_use"
        fake_tool_use.name = "done"
        fake_tool_use.id = "tu_001"
        fake_tool_use.input = {"summary": "finished"}

        fake_response = MagicMock()
        fake_response.stop_reason = "tool_use"
        fake_response.content = [fake_tool_use]

        with patch("coding_agent.claude_agent.anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = fake_response

            result = run_agent("title", "desc", str(tmp_path), api_key="sk-ant-fake")
            assert result == "finished"
            assert mock_client.messages.create.call_count == 1

    def test_end_turn_stop_returns_text(self, tmp_path):
        """Agent ends naturally without calling done."""
        fake_text = MagicMock()
        fake_text.type = "text"
        fake_text.text = "I'm done here"

        fake_response = MagicMock()
        fake_response.stop_reason = "end_turn"
        fake_response.content = [fake_text]

        with patch("coding_agent.claude_agent.anthropic.Anthropic") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.messages.create.return_value = fake_response

            result = run_agent("title", "desc", str(tmp_path), api_key="sk-ant-fake")
            assert "done" in result.lower()
