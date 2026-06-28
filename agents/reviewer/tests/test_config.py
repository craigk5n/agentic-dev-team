"""Tests for reviewer.config.Settings derived properties."""

from reviewer.config import Settings


def test_effective_forgejo_token_prefers_reviewer_bot():
    s = Settings(forgejo_reviewer_token="rev-tok", forgejo_api_token="admin-tok")
    assert s.effective_forgejo_token == "rev-tok"


def test_effective_forgejo_token_falls_back_to_admin():
    s = Settings(forgejo_reviewer_token="", forgejo_api_token="admin-tok")
    assert s.effective_forgejo_token == "admin-tok"


def test_effective_api_key_prefers_openrouter():
    s = Settings(openrouter_api_key="or", anthropic_api_key="an")
    assert s.effective_api_key == "or"
