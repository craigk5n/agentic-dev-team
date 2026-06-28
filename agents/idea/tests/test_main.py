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
