"""Tests for coding_agent.config — least-privilege coder-bot token selection."""

from coding_agent.config import Settings


def test_effective_token_prefers_coder_bot():
    s = Settings(forgejo_coder_token="coder-tok", forgejo_api_token="admin-tok")
    assert s.effective_forgejo_token == "coder-tok"
    assert s.effective_forgejo_user == "coder-bot"


def test_effective_token_falls_back_to_admin():
    s = Settings(forgejo_coder_token="", forgejo_api_token="admin-tok")
    assert s.effective_forgejo_token == "admin-tok"
    assert s.effective_forgejo_user == "devadmin"


def test_custom_coder_user_honored():
    s = Settings(forgejo_coder_token="t", forgejo_coder_user="bot2")
    assert s.effective_forgejo_user == "bot2"
