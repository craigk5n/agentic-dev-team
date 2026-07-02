"""Tests for idea expansion generator."""

import json
from unittest.mock import MagicMock, patch

import pytest

from idea_agent.generator import expand_prompt, description_to_html, _parse


def _mock_llm(text: str):
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class TestExpandPrompt:
    def test_returns_title_and_description(self):
        payload = {"title": "JWT Auth", "description": "## Overview\n\nBuild auth."}
        with patch("idea_agent.generator.litellm.completion", return_value=_mock_llm(json.dumps(payload))):
            result = expand_prompt("Add auth", model="m")
        assert result["title"] == "JWT Auth"
        assert "description" in result

    def test_passes_api_key(self):
        payload = {"title": "T", "description": "D"}
        with patch("idea_agent.generator.litellm.completion", return_value=_mock_llm(json.dumps(payload))) as mock:
            expand_prompt("prompt", model="m", api_key="sk-test")
        assert mock.call_args[1]["api_key"] == "sk-test"

    def test_no_api_key_not_passed(self):
        payload = {"title": "T", "description": "D"}
        with patch("idea_agent.generator.litellm.completion", return_value=_mock_llm(json.dumps(payload))) as mock:
            expand_prompt("prompt", model="m")
        assert "api_key" not in mock.call_args[1]

    def test_tolerates_markdown_fences(self):
        payload = {"title": "T", "description": "D"}
        raw = f"```json\n{json.dumps(payload)}\n```"
        with patch("idea_agent.generator.litellm.completion", return_value=_mock_llm(raw)):
            result = expand_prompt("prompt", model="m")
        assert result["title"] == "T"

    def test_invalid_json_falls_back(self):
        with patch("idea_agent.generator.litellm.completion", return_value=_mock_llm("not json")):
            result = expand_prompt("my feature request", model="m")
        # fallback: uses prompt as title (truncated to 80 chars)
        assert "my feature request" in result["title"]


class TestParse:
    def test_valid_json(self):
        raw = '{"title": "T", "description": "D"}'
        assert _parse(raw, "fallback")["title"] == "T"

    def test_missing_title_falls_back(self):
        raw = '{"only_description": "D"}'
        result = _parse(raw, "fallback prompt")
        assert result["title"] == "fallback prompt"

    def test_json_error_falls_back(self):
        result = _parse("{bad json", "fallback prompt")
        assert result["title"] == "fallback prompt"


class TestDescriptionToHtml:
    def test_converts_heading(self):
        html = description_to_html("## Overview")
        assert "<h2>Overview</h2>" in html

    def test_converts_list_item(self):
        html = description_to_html("- Build auth")
        assert "<li>Build auth</li>" in html

    def test_converts_paragraph(self):
        html = description_to_html("Plain text line.")
        assert "<p>Plain text line.</p>" in html

    def test_empty_lines_skipped(self):
        html = description_to_html("## H\n\n- item")
        assert "<p></p>" not in html


class TestExpandRetry:
    _GOOD = {"title": "JWT Auth", "description": "## Overview\n\nBuild auth."}

    def test_sets_timeout_and_disables_internal_retry(self):
        with patch("idea_agent.generator.litellm.completion",
                   return_value=_mock_llm(json.dumps(self._GOOD))) as mock:
            expand_prompt("Add auth", model="m")
        assert mock.call_args[1]["timeout"] == 90.0
        assert mock.call_args[1]["num_retries"] == 0

    def test_retries_empty_then_succeeds(self):
        with patch("idea_agent.generator.litellm.completion",
                   side_effect=[_mock_llm(""), _mock_llm(json.dumps(self._GOOD))]) as mock, \
             patch("idea_agent.generator.time.sleep"):
            result = expand_prompt("Add auth", model="m")
        assert result["title"] == "JWT Auth" and mock.call_count == 2

    def test_falls_back_after_all_attempts_fail(self):
        with patch("idea_agent.generator.litellm.completion",
                   side_effect=Exception("provider down")) as mock, \
             patch("idea_agent.generator.time.sleep"):
            result = expand_prompt("Add auth", model="m")
        assert mock.call_count == 3                 # initial + 2 retries
        assert result["title"] == "Add auth"        # graceful fallback


class TestClaudeCodeRouting:
    _GOOD = {"title": "JWT Auth", "description": "## Overview\n\nBuild auth."}

    def test_claude_code_model_bypasses_litellm(self):
        # Subscription models route via the shared CLI adapter — litellm never called.
        with patch("planner_agent.claude_code.complete",
                   return_value=json.dumps(self._GOOD)) as cli, \
             patch("idea_agent.generator.litellm.completion") as llm:
            result = expand_prompt("Add auth", model="claude-code/opus")
        assert llm.call_count == 0
        assert cli.call_count == 1
        assert cli.call_args.kwargs["model"] == "claude-code/opus"
        assert result["title"] == "JWT Auth"

    def test_adapter_failure_falls_back_gracefully(self):
        with patch("planner_agent.claude_code.complete",
                   side_effect=RuntimeError("no token")) as cli, \
             patch("idea_agent.generator.litellm.completion") as llm, \
             patch("idea_agent.generator.time.sleep"):
            result = expand_prompt("Add auth", model="claude-code")
        assert llm.call_count == 0
        assert cli.call_count == 3                # full retry budget
        assert result["title"] == "Add auth"     # prompt-as-title fallback
