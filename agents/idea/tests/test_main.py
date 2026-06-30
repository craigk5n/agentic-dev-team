"""Tests for the Idea Agent orchestrator."""

import pytest
from unittest.mock import MagicMock, patch

from idea_agent.main import expand_idea, submit_idea


class TestSubmitIdea:
    def test_returns_pending_approval_status(self):
        proposal = {"title": "JWT Auth", "description": "## Overview\n\nBuild auth."}
        with patch("idea_agent.main.expand_prompt", return_value=proposal):
            result = submit_idea("Add JWT auth")
        assert result["status"] == "pending_approval"
        assert result["title"] == "JWT Auth"
        assert result["proposal"] == proposal

    def test_model_override_passed_to_expand_prompt(self):
        proposal = {"title": "T", "description": "D"}
        with patch("idea_agent.main.expand_prompt", return_value=proposal) as mock:
            expand_idea("my prompt", model_override="gpt-4o")
        assert mock.call_args.kwargs["model"] == "gpt-4o"

    def test_default_model_from_settings(self):
        proposal = {"title": "T", "description": "D"}
        with patch("idea_agent.main.expand_prompt", return_value=proposal) as mock, \
             patch("idea_agent.main.settings") as mock_settings:
            mock_settings.model_idea = "default-model"
            mock_settings.effective_api_key = "key"
            expand_idea("my prompt")
        assert mock.call_args.kwargs["model"] == "default-model"

    def test_project_id_and_workspace_accepted_but_ignored(self):
        proposal = {"title": "T", "description": "D"}
        with patch("idea_agent.main.expand_prompt", return_value=proposal):
            result = submit_idea("prompt", project_id="proj-1", workspace_slug="ws")
        assert result["status"] == "pending_approval"

    def test_submit_idea_passes_model_override(self):
        proposal = {"title": "T", "description": "D"}
        with patch("idea_agent.main.expand_prompt", return_value=proposal) as mock:
            submit_idea("prompt", model_override="gpt-4o")
        assert mock.call_args.kwargs["model"] == "gpt-4o"


# ── EPIC 2: stack/SDLC proposal ───────────────────────────────────────────────

class TestStackProposal:
    def test_options_forwarded_to_expand_prompt(self):
        from idea_agent.main import expand_idea
        from unittest.mock import patch
        stacks = [{"id": "python", "display_name": "Python"}]
        sdlc = [{"id": "tdd", "display_name": "TDD"}]
        with patch("idea_agent.main.expand_prompt",
                   return_value={"title": "T", "description": "D"}) as mock:
            expand_idea("p", stack_options=stacks, sdlc_options=sdlc)
        assert mock.call_args.kwargs["stack_options"] == stacks
        assert mock.call_args.kwargs["sdlc_options"] == sdlc

    def test_parse_passes_through_proposal_fields(self):
        from idea_agent.generator import _parse
        raw = ('{"title":"T","description":"D","proposed_stack":"go",'
               '"proposed_sdlc":"tdd","stack_rationale":"because"}')
        out = _parse(raw, "fallback")
        assert out["proposed_stack"] == "go"
        assert out["proposed_sdlc"] == "tdd"
        assert out["stack_rationale"] == "because"

    def test_guidance_omitted_without_options(self):
        from idea_agent.generator import _stack_guidance
        assert _stack_guidance([], []) == ""

    def test_guidance_lists_stack_ids(self):
        from idea_agent.generator import _stack_guidance
        g = _stack_guidance([{"id": "go", "display_name": "Go"}],
                            [{"id": "tdd", "display_name": "TDD"}])
        assert "go" in g and "tdd" in g and "generic" in g


class TestStyleGuideProposal:
    def test_options_forwarded_and_field_present(self):
        from idea_agent.generator import _style_guidance, _STYLE_FIELD
        g = _style_guidance([{"id": "google-python", "display_name": "Google Python"}])
        assert "google-python" in g
        assert "proposed_style_guides" in _STYLE_FIELD

    def test_expand_forwards_style_options(self):
        from idea_agent.main import expand_idea
        from unittest.mock import patch
        opts = [{"id": "human-voice", "display_name": "Human"}]
        with patch("idea_agent.main.expand_prompt",
                   return_value={"title": "T", "description": "D"}) as mock:
            expand_idea("p", style_guide_options=opts)
        assert mock.call_args.kwargs["style_guide_options"] == opts
