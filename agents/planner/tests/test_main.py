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


# ── EPIC 4: SDLC-aware decomposition ──────────────────────────────────────────

class TestSdlcAwarePlanning:
    def test_run_planner_forwards_directive_and_practices(self):
        _PLAN = {"module_name": "M", "module_description": "d", "stories": []}
        with patch("planner_agent.main.decompose_idea", return_value=_PLAN) as mock:
            run_planner("idea-1", "T", "D", sdlc_directive="TESTS FIRST",
                        best_practices="idiomatic go")
        assert mock.call_args.kwargs["sdlc_directive"] == "TESTS FIRST"
        assert mock.call_args.kwargs["best_practices"] == "idiomatic go"

    def test_style_block_includes_both(self):
        from planner_agent.decomposer import _style_block
        block = _style_block("write failing tests first", "use type hints")
        assert "write failing tests first" in block
        assert "use type hints" in block
        assert "ORDERING" in block

    def test_style_block_empty_when_none(self):
        from planner_agent.decomposer import _style_block
        assert _style_block("", "") == ""

    def test_decompose_injects_directive_into_prompt(self):
        from unittest.mock import MagicMock
        import planner_agent.decomposer as dec
        captured = {}

        def fake_completion(**kwargs):
            captured["content"] = kwargs["messages"][1]["content"]
            r = MagicMock()
            r.choices[0].message.content = '{"module_name":"M","module_description":"d","stories":[]}'
            return r

        with patch.object(dec.litellm, "completion", side_effect=fake_completion):
            dec.decompose_idea("T", "D", model="m", sdlc_directive="TDD_DIRECTIVE",
                               best_practices="GO_CONV")
        assert "TDD_DIRECTIVE" in captured["content"]
        assert "GO_CONV" in captured["content"]
