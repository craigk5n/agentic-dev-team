"""Tests for the two-level idea decomposer (epics → stories → critic)."""

import json
from unittest.mock import MagicMock, patch

from planner_agent.decomposer import decompose_idea, normalize_plan


def _mock_llm(text: str):
    msg = MagicMock(); msg.content = text
    choice = MagicMock(); choice.message = msg
    resp = MagicMock(); resp.choices = [choice]
    return resp


def _seq(*payloads):
    """A side_effect list of mock LLM responses, one per expected call."""
    return [_mock_llm(json.dumps(p) if not isinstance(p, str) else p) for p in payloads]


_EPICS = {"project_name": "Auth Module", "epics": [
    {"name": "Password hashing", "description": "Securely hash + verify passwords."},
    {"name": "Tokens", "description": "Issue and validate JWTs."},
]}
_STORIES_A = {"stories": [{"title": "Hash on signup", "description": "repo: dev/app\nHash.", "priority": "high"}]}
_STORIES_B = {"stories": [{"title": "Issue JWT", "description": "repo: dev/app\nJWT.", "priority": "medium"}]}
_NO_MISSING = {"missing": []}


class TestTwoLevelPlan:
    def test_epics_then_stories_flattened_and_tagged(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, _NO_MISSING)):
            plan = decompose_idea("Auth", "Build auth", model="m")
        assert plan["project_name"] == "Auth Module"
        assert [e["name"] for e in plan["epics"]] == ["Password hashing", "Tokens"]
        # stories are flattened epic-by-epic and tagged with their epic
        assert [s["title"] for s in plan["stories"]] == ["Hash on signup", "Issue JWT"]
        assert plan["stories"][0]["epic"] == "Password hashing"
        assert plan["stories"][1]["epic"] == "Tokens"

    def test_completeness_critic_adds_missing_stories(self):
        crit = {"missing": [{"epic": "Tokens", "title": "Refresh tokens",
                             "description": "repo: dev/app\nRotate.", "priority": "high"}]}
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, crit)):
            plan = decompose_idea("Auth", "Build auth", model="m")
        titles = [s["title"] for s in plan["stories"]]
        assert "Refresh tokens" in titles
        # appended under its epic
        assert next(s for s in plan["stories"] if s["title"] == "Refresh tokens")["epic"] == "Tokens"

    def test_critic_can_introduce_a_new_epic(self):
        crit = {"missing": [{"epic": "Accessibility", "title": "Keyboard nav",
                             "description": "repo: dev/app\nA11y.", "priority": "low"}]}
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, crit)):
            plan = decompose_idea("Auth", "Build auth", model="m")
        assert "Accessibility" in [e["name"] for e in plan["epics"]]

    def test_epics_prompt_orders_foundational_first_and_scopes(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, _NO_MISSING)) as mock:
            decompose_idea("Auth", "Build auth", model="m")
        epics_prompt = mock.call_args_list[0][1]["messages"][1]["content"].lower()
        assert "foundational-first" in epics_prompt
        assert "cover the full scope" in epics_prompt

    def test_stories_prompt_forbids_stubs_and_pins_repo(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, _NO_MISSING)) as mock:
            decompose_idea("Auth", "Build auth", model="m", default_repo="alice/backend")
        stories_prompt = mock.call_args_list[1][1]["messages"][1]["content"].lower()
        assert "already scaffolded" in stories_prompt
        assert "signature" in stories_prompt
        assert "alice/backend" in stories_prompt

    def test_repo_prefix_enforced_on_stories(self):
        no_prefix = {"stories": [{"title": "X", "description": "do the thing", "priority": "low"}]}
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, no_prefix, _STORIES_B, _NO_MISSING)):
            plan = decompose_idea("Auth", "Build auth", model="m", default_repo="o/r")
        assert plan["stories"][0]["description"].startswith("repo: o/r")

    def test_api_key_passed_through(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, _NO_MISSING)) as mock:
            decompose_idea("T", "D", model="m", api_key="sk-test")
        assert mock.call_args_list[0][1]["api_key"] == "sk-test"

    def test_locked_decisions_injected_into_prompts(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(_EPICS, _STORIES_A, _STORIES_B, _NO_MISSING)) as mock:
            decompose_idea("Auth", "Build auth", model="m",
                           decisions="LOCKED: storage=Postgres; auth=JWT")
        for call in mock.call_args_list:                 # rides on every planning prompt
            assert "storage=Postgres" in call[1]["messages"][1]["content"]

    def test_fallback_when_epics_pass_fails(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq("{bad json")):
            plan = decompose_idea("My Feature", "D", model="m", default_repo="devadmin/sandbox")
        assert plan["module_name"].startswith("My Feature")
        assert len(plan["stories"]) == 1
        assert plan["stories"][0]["description"].startswith("repo: devadmin/sandbox")


class TestNormalizePlan:
    _NORM = {"project_name": "Task API", "epics": [
        {"name": "Data Model", "description": "models + storage"},
        {"name": "Endpoints", "description": "REST"}],
        "stories": [
            {"title": "Task model", "epic": "Data Model", "description": "repo: o/r\nmodel", "priority": "high"},
            {"title": "CRUD endpoints", "epic": "Endpoints", "description": "endpoints", "priority": "medium"},
        ]}

    def test_maps_pasted_plan_to_epic_story_model(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._NORM)):
            plan = normalize_plan("## Epic 1 ...\n### Story ...", model="m", default_repo="o/r")
        assert plan["project_name"] == "Task API"
        assert [e["name"] for e in plan["epics"]] == ["Data Model", "Endpoints"]
        assert [s["title"] for s in plan["stories"]] == ["Task model", "CRUD endpoints"]
        assert plan["stories"][0]["epic"] == "Data Model"
        # repo prefix enforced even when the model omits it
        assert plan["stories"][1]["description"].startswith("repo: o/r")

    def test_reconciliation_guidance_in_prompt(self):
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=_seq(self._NORM)) as mock:
            normalize_plan("some plan text", model="m", default_repo="o/r")
        prompt = mock.call_args[1]["messages"][1]["content"].lower()
        assert "already scaffolded" in prompt          # drops setup stories
        assert "re-structuring an existing plan" in prompt

    def test_fallback_on_unparseable(self):
        with patch("planner_agent.decomposer.litellm.completion", side_effect=_seq("{bad")):
            plan = normalize_plan("x" * 100, model="m", default_repo="devadmin/sandbox")
        assert len(plan["stories"]) == 1
        assert plan["stories"][0]["description"].startswith("repo: devadmin/sandbox")


class TestCallHardening:
    def test_complete_json_sets_timeout_and_disables_internal_retry(self):
        from planner_agent.decomposer import _complete_json
        with patch("planner_agent.decomposer.litellm.completion",
                   return_value=_mock_llm('{"ok": 1}')) as mock:
            out = _complete_json([{"role": "user", "content": "x"}],
                                 model="m", api_key="", stack="", redis_conn=None)
        assert out == {"ok": 1}
        assert mock.call_args[1]["timeout"] == 90.0
        assert mock.call_args[1]["num_retries"] == 0

    def test_complete_json_retries_transient_then_succeeds(self):
        from planner_agent.decomposer import _complete_json
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=[Exception("provider error"), _mock_llm('{"ok": 1}')]) as mock, \
             patch("planner_agent.decomposer.time.sleep"):
            out = _complete_json([{"role": "user", "content": "x"}],
                                 model="m", api_key="", stack="", redis_conn=None)
        assert out == {"ok": 1} and mock.call_count == 2

    def test_complete_json_retries_empty_content(self):
        from planner_agent.decomposer import _complete_json
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=[_mock_llm(""), _mock_llm('{"ok": 1}')]) as mock, \
             patch("planner_agent.decomposer.time.sleep"):
            out = _complete_json([{"role": "user", "content": "x"}],
                                 model="m", api_key="", stack="", redis_conn=None)
        assert out == {"ok": 1} and mock.call_count == 2

    def test_complete_json_raises_after_max_attempts(self):
        import pytest
        from planner_agent.decomposer import _complete_json
        with patch("planner_agent.decomposer.litellm.completion",
                   side_effect=Exception("down")) as mock, \
             patch("planner_agent.decomposer.time.sleep"):
            with pytest.raises(Exception):
                _complete_json([{"role": "user", "content": "x"}],
                               model="m", api_key="", stack="", redis_conn=None)
        assert mock.call_count == 3   # initial + 2 retries
