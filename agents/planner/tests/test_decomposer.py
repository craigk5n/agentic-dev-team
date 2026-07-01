"""Tests for the idea decomposer."""

import json
from unittest.mock import MagicMock, patch

import pytest

from planner_agent.decomposer import decompose_idea, _parse


def _mock_llm(text: str):
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


_VALID = {
    "module_name": "Auth Module",
    "module_description": "Full authentication system",
    "stories": [
        {"title": "Password hashing", "description": "repo: dev/app\nHash passwords.", "priority": "high"},
        {"title": "JWT tokens", "description": "repo: dev/app\nIssue JWTs.", "priority": "medium"},
    ],
}


class TestDecomposeIdea:
    def test_returns_structured_plan(self):
        with patch("planner_agent.decomposer.litellm.completion", return_value=_mock_llm(json.dumps(_VALID))):
            result = decompose_idea("Auth", "Build auth", model="m")
        assert result["module_name"] == "Auth Module"
        assert len(result["stories"]) == 2

    def test_passes_api_key(self):
        with patch("planner_agent.decomposer.litellm.completion", return_value=_mock_llm(json.dumps(_VALID))) as mock:
            decompose_idea("T", "D", model="m", api_key="sk-test")
        assert mock.call_args[1]["api_key"] == "sk-test"

    def test_no_api_key_not_passed(self):
        with patch("planner_agent.decomposer.litellm.completion", return_value=_mock_llm(json.dumps(_VALID))) as mock:
            decompose_idea("T", "D", model="m")
        assert "api_key" not in mock.call_args[1]

    def test_default_repo_in_prompt(self):
        with patch("planner_agent.decomposer.litellm.completion", return_value=_mock_llm(json.dumps(_VALID))) as mock:
            decompose_idea("T", "D", model="m", default_repo="alice/backend")
        prompt_content = mock.call_args[1]["messages"][1]["content"]
        assert "alice/backend" in prompt_content

    def test_prompt_forbids_stub_only_stories(self):
        # The repo is already scaffolded at provisioning; the planner must not emit
        # signature-only / setup-only first stories (they can't pass review — the
        # function is unimplemented — and churn the recode loop until it's parked).
        with patch("planner_agent.decomposer.litellm.completion", return_value=_mock_llm(json.dumps(_VALID))) as mock:
            decompose_idea("T", "D", model="m")
        prompt = mock.call_args[1]["messages"][1]["content"].lower()
        assert "already scaffolded" in prompt
        assert "signature" in prompt  # explicit no-signature-only rule

    def test_tolerates_markdown_fences(self):
        raw = f"```json\n{json.dumps(_VALID)}\n```"
        with patch("planner_agent.decomposer.litellm.completion", return_value=_mock_llm(raw)):
            result = decompose_idea("T", "D", model="m")
        assert result["module_name"] == "Auth Module"

    def test_description_truncated(self):
        long_desc = "x" * 5000
        with patch("planner_agent.decomposer.litellm.completion", return_value=_mock_llm(json.dumps(_VALID))) as mock:
            decompose_idea("T", long_desc, model="m")
        prompt = mock.call_args[1]["messages"][1]["content"]
        # The 5000-char description must be truncated to 4000 chars in the prompt.
        # Verify no run of 4001+ consecutive x's exists.
        assert "x" * 4001 not in prompt


class TestParse:
    def test_valid_json(self):
        raw = json.dumps(_VALID)
        result = _parse(raw, "title", "repo")
        assert result["module_name"] == "Auth Module"

    def test_missing_fields_falls_back(self):
        raw = '{"only_module": "M"}'
        result = _parse(raw, "My Feature", "devadmin/sandbox")
        assert result["module_name"] == "My Feature"
        assert len(result["stories"]) == 1

    def test_json_error_falls_back(self):
        result = _parse("{bad json", "My Feature", "devadmin/sandbox")
        assert "My Feature" in result["module_name"]
        assert "devadmin/sandbox" in result["stories"][0]["description"]
