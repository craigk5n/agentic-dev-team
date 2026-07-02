"""Tests for the litellm wrapper."""

import json
from unittest.mock import MagicMock, patch

import pytest

from reviewer.llm import complete, review_diff, summarise_test_output, _parse_json_response


def _mock_completion(text: str):
    msg = MagicMock()
    msg.content = text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp


class TestComplete:
    def test_returns_content(self):
        with patch("reviewer.llm.litellm.completion", return_value=_mock_completion("hello")) as mock:
            result = complete("openrouter/anthropic/claude-haiku-4-5", [{"role": "user", "content": "hi"}])
        assert result == "hello"
        mock.assert_called_once()

    def test_passes_api_key(self):
        with patch("reviewer.llm.litellm.completion", return_value=_mock_completion("x")) as mock:
            complete("model", [{"role": "user", "content": "q"}], api_key="sk-test")
        call_kwargs = mock.call_args[1]
        assert call_kwargs["api_key"] == "sk-test"

    def test_no_api_key_not_passed(self):
        with patch("reviewer.llm.litellm.completion", return_value=_mock_completion("x")) as mock:
            complete("model", [{"role": "user", "content": "q"}])
        call_kwargs = mock.call_args[1]
        assert "api_key" not in call_kwargs

    def test_structured_sends_response_format_when_supported(self):
        with patch("reviewer.llm.litellm.completion", return_value=_mock_completion("{}")) as mock, \
             patch("reviewer.llm._supports_structured", return_value=True):
            complete("m", [{"role": "user", "content": "q"}], structured=True)
        assert mock.call_args[1].get("response_format") == {"type": "json_object"}

    def test_structured_skipped_when_model_unsupported(self):
        with patch("reviewer.llm.litellm.completion", return_value=_mock_completion("{}")) as mock, \
             patch("reviewer.llm._supports_structured", return_value=False):
            complete("m", [{"role": "user", "content": "q"}], structured=True)
        assert "response_format" not in mock.call_args[1]

    def test_no_response_format_when_structured_false(self):
        with patch("reviewer.llm.litellm.completion", return_value=_mock_completion("x")) as mock, \
             patch("reviewer.llm._supports_structured", return_value=True):
            complete("m", [{"role": "user", "content": "q"}])   # structured defaults False
        assert "response_format" not in mock.call_args[1]

    def test_review_diff_requests_structured(self):
        with patch("reviewer.llm.litellm.completion",
                   return_value=_mock_completion('{"status":"pass","summary":"ok","findings":[]}')) as mock, \
             patch("reviewer.llm._supports_structured", return_value=True):
            review_diff("some diff", model="m")
        assert mock.call_args[1].get("response_format") == {"type": "json_object"}


class TestReviewDiff:
    def test_returns_parsed_verdict(self):
        verdict = {"status": "pass", "summary": "Looks good", "findings": []}
        with patch("reviewer.llm.complete", return_value=json.dumps(verdict)):
            result = review_diff("some diff", model="m", api_key="k")
        assert result["status"] == "pass"

    def test_truncates_large_diff(self):
        large_diff = "x" * 30_000
        with patch("reviewer.llm.complete", return_value='{"status":"pass","summary":"ok","findings":[]}') as mock:
            review_diff(large_diff, model="m")
        call_args = mock.call_args[0]
        prompt = call_args[1][1]["content"]
        assert len(prompt) < 30_000

    def test_tolerates_markdown_fences(self):
        raw = "```json\n{\"status\":\"pass\",\"summary\":\"ok\",\"findings\":[]}\n```"
        with patch("reviewer.llm.complete", return_value=raw):
            result = review_diff("diff", model="m")
        assert result["status"] == "pass"


class TestSummariseTestOutput:
    def test_pass_result(self):
        verdict = {"status": "pass", "summary": "All passed", "failures": []}
        with patch("reviewer.llm.complete", return_value=json.dumps(verdict)):
            result = summarise_test_output("1 passed", model="m")
        assert result["status"] == "pass"


class TestParseJsonResponse:
    def test_clean_json(self):
        assert _parse_json_response('{"a":1}', role="x") == {"a": 1}

    def test_fenced_json(self):
        raw = "```json\n{\"a\":1}\n```"
        assert _parse_json_response(raw, role="x") == {"a": 1}

    def test_invalid_json_returns_warn(self):
        result = _parse_json_response("not json", role="x")
        assert result["status"] == "warn"
