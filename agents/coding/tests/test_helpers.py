"""Tests for pure-logic helpers in the Coding Agent."""

import pytest
from coding_agent.main import _branch_name, _extract_repo
from coding_agent.claude_agent import _safe_path


_UUID = "abc12345-def0-0000-0000-000000000000"


class TestBranchName:
    def test_normal_title(self):
        name = _branch_name(_UUID, "Add login page")
        assert name == "story-abc12345/add-login-page"

    def test_special_chars_stripped(self):
        name = _branch_name(_UUID, "Fix: user's email (validation)")
        assert "//" not in name
        assert name.startswith("story-abc12345/")

    def test_long_title_truncated(self):
        title = "A" * 100
        name = _branch_name(_UUID, title)
        slug = name.split("/", 1)[1]
        assert len(slug) <= 40

    def test_short_item_id(self):
        # UUIDs always have at least 8 chars; short IDs still work
        name = _branch_name("abc", "Quick fix")
        assert name == "story-abc/quick-fix"


class TestExtractRepo:
    def test_extracts_repo_directive(self):
        desc = "Do some work.\nrepo: alice/backend\nMore text."
        assert _extract_repo(desc) == "alice/backend"

    def test_case_sensitive_owner(self):
        desc = "repo: Alice/Backend"
        assert _extract_repo(desc) == "Alice/Backend"

    def test_no_directive_returns_none(self):
        assert _extract_repo("Just a description.") is None

    def test_none_description_returns_none(self):
        assert _extract_repo(None) is None

    def test_inline_not_matched(self):
        # 'repo:' must be at the start of a line
        desc = "See repo: alice/backend for details"
        assert _extract_repo(desc) is None


class TestSafePath:
    def test_allows_normal_path(self, tmp_path):
        p = _safe_path(str(tmp_path), "src/main.py")
        assert str(p).startswith(str(tmp_path))

    def test_blocks_path_traversal(self, tmp_path):
        with pytest.raises(ValueError, match="escapes"):
            _safe_path(str(tmp_path), "../etc/passwd")

    def test_blocks_absolute_path_escape(self, tmp_path):
        with pytest.raises(ValueError, match="escapes"):
            _safe_path(str(tmp_path), "/etc/passwd")

    def test_allows_nested_path(self, tmp_path):
        p = _safe_path(str(tmp_path), "a/b/c/d.txt")
        assert str(p) == str(tmp_path / "a" / "b" / "c" / "d.txt")
