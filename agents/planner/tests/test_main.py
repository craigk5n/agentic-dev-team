"""Tests for the Planner Agent orchestrator."""

from unittest.mock import patch

from planner_agent.main import run_planner


_PLAN = {
    "module_name": "Auth Module",
    "module_description": "All auth work",
    "stories": [
        {"title": "Hash passwords", "description": "Hash them.", "priority": "high"},
        {"title": "Issue JWTs", "description": "Issue tokens.", "priority": "medium"},
    ],
}


class TestRunPlanner:
    def test_returns_plan_from_decomposer(self):
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN):
            result = run_planner("idea-1", "Auth System", "Build auth.")
        assert result == _PLAN

    def test_passes_title_and_description_to_decomposer(self):
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN) as mock:
            run_planner("idea-1", "Auth System", "Build the auth system.")
        args = mock.call_args.args
        assert args[0] == "Auth System"
        assert args[1] == "Build the auth system."

    def test_model_override_passed_to_decomposer(self):
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN) as mock:
            run_planner("idea-1", "T", "D", model_override="gpt-4o")
        assert mock.call_args.kwargs["model"] == "gpt-4o"

    def test_uses_settings_model_when_no_override(self):
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN) as mock, \
             patch("planner_agent.main.settings") as mock_settings:
            mock_settings.model_planner = "settings-model"
            mock_settings.effective_api_key = "key"
            mock_settings.default_repo = "dev/sandbox"
            run_planner("idea-1", "T", "D")
        assert mock.call_args.kwargs["model"] == "settings-model"

    def test_repo_full_name_passed_as_default_repo(self):
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN) as mock:
            run_planner("idea-1", "T", "D", repo_full_name="alice/backend")
        assert mock.call_args.kwargs["default_repo"] == "alice/backend"
