"""Tests for the code review agent (all I/O mocked)."""

import pytest
from unittest.mock import MagicMock, patch
import fakeredis


def _make_forgejo():
    fg = MagicMock()
    fg.__enter__ = lambda s: s
    fg.__exit__ = MagicMock(return_value=False)
    fg.post_pr_comment.return_value = {"id": 1}
    return fg


class TestRunCodeReview:
    def test_success_posts_comment(self, tmp_path):
        verdict = {"status": "pass", "summary": "ok", "findings": []}
        r = fakeredis.FakeRedis()

        with (
            patch("reviewer.code_review.git_ops.clone"),
            patch("reviewer.code_review.git_ops.get_diff", return_value="+ new line"),
            patch("reviewer.code_review.llm.review_diff", return_value=verdict),
            patch("reviewer.code_review.ForgejoClient", return_value=_make_forgejo()),
            patch("reviewer.code_review.redis.from_url", return_value=r),
        ):
            from reviewer.code_review import run_code_review
            result = run_code_review("alice/backend", 7, "a" * 40)

        assert result["status"] == "pass"
        assert result["role"] == "code_review"

    def test_insufficient_credits_returns_error_no_verdict(self):
        # An out-of-credit failure returns a distinct error and does NOT store a verdict
        # or post a review comment — the worker surfaces it to the operator instead.
        from reviewer.llm import InsufficientCreditsError
        r = fakeredis.FakeRedis()
        fg = _make_forgejo()
        with (
            patch("reviewer.code_review.git_ops.clone"),
            patch("reviewer.code_review.git_ops.get_diff", return_value="+ x"),
            patch("reviewer.code_review.llm.review_diff",
                  side_effect=InsufficientCreditsError("requires more credits")),
            patch("reviewer.code_review.ForgejoClient", return_value=fg),
            patch("reviewer.code_review.redis.from_url", return_value=r),
            patch("reviewer.code_review.store_and_check") as mock_store,
        ):
            from reviewer.code_review import run_code_review
            result = run_code_review("alice/backend", 7, "a" * 40)
        assert result["status"] == "error"
        assert result["reason"] == "insufficient_credits"
        assert result.get("operator_action") is True
        mock_store.assert_not_called()          # no poisoned verdict stored
        fg.post_pr_comment.assert_not_called()  # no misleading review comment

    def test_empty_diff_skips_llm(self, tmp_path):
        r = fakeredis.FakeRedis()

        with (
            patch("reviewer.code_review.git_ops.clone"),
            patch("reviewer.code_review.git_ops.get_diff", return_value="  "),
            patch("reviewer.code_review.llm.review_diff") as mock_llm,
            patch("reviewer.code_review.ForgejoClient", return_value=_make_forgejo()),
            patch("reviewer.code_review.redis.from_url", return_value=r),
        ):
            from reviewer.code_review import run_code_review
            result = run_code_review("alice/backend", 7, "a" * 40)

        mock_llm.assert_not_called()
        assert result["status"] == "pass"

    def test_aggregation_posted_when_all_in(self):
        """When all 3 verdicts arrive, the aggregated summary is posted and gate runs."""
        from reviewer.verdicts import store_verdict
        r = fakeredis.FakeRedis()
        # Pre-fill the other 2 roles so code_review wins the aggregation race
        store_verdict(r, "alice/backend", 7, "test_run", {"status": "pass", "summary": "ok"})
        store_verdict(r, "alice/backend", 7, "security", {"status": "pass", "summary": "ok"})

        fg = _make_forgejo()
        fg.merge_pr = MagicMock(return_value={"merged": True})
        # Patch at the source so verdicts.py and gate.py both see the mock
        with (
            patch("reviewer.code_review.git_ops.clone"),
            patch("reviewer.code_review.git_ops.get_diff", return_value="diff"),
            patch("reviewer.code_review.llm.review_diff", return_value={"status": "pass", "summary": "ok", "findings": []}),
            patch("reviewer.code_review.ForgejoClient", return_value=fg),
            patch("reviewer.forgejo_client.ForgejoClient", return_value=fg),
            patch("reviewer.code_review.redis.from_url", return_value=r),
        ):
            from reviewer.code_review import run_code_review
            run_code_review("alice/backend", 7, "a" * 40)

        # post_pr_comment: once for the code-review result, once for aggregated summary
        assert fg.post_pr_comment.call_count >= 2


class TestDeletionGuardIntegration:
    def _forgejo_with_files(self, files, pr):
        fg = _make_forgejo()
        fg.get_pr_files.return_value = files
        fg.get_pr.return_value = pr
        return fg

    def test_unexpected_deletion_forces_fail(self):
        # LLM passes, but the PR deletes a tracked file and the story is not a refactor →
        # the verdict is forced to fail (blocked).
        r = fakeredis.FakeRedis()
        fg = self._forgejo_with_files(
            [{"filename": "Dockerfile", "status": "deleted", "additions": 0, "deletions": 20}],
            {"title": "Add health endpoint", "body": "repo: alice/backend"},
        )
        with (
            patch("reviewer.code_review.git_ops.clone"),
            patch("reviewer.code_review.git_ops.get_diff", return_value="diff --git"),
            patch("reviewer.code_review.llm.review_diff",
                  return_value={"status": "pass", "summary": "looks fine", "findings": []}),
            patch("reviewer.code_review.ForgejoClient", return_value=fg),
            patch("reviewer.code_review.redis.from_url", return_value=r),
        ):
            from reviewer.code_review import run_code_review
            result = run_code_review("alice/backend", 7, "a" * 40)

        assert result["status"] == "fail"
        assert any(f.get("severity") == "critical" for f in result["findings"])
        assert "Dockerfile" in result["findings"][0]["message"]

    def test_expected_deletion_does_not_block(self):
        r = fakeredis.FakeRedis()
        fg = self._forgejo_with_files(
            [{"filename": "legacy.py", "status": "deleted", "additions": 0, "deletions": 30}],
            {"title": "Refactor: remove legacy adapter", "body": ""},
        )
        with (
            patch("reviewer.code_review.git_ops.clone"),
            patch("reviewer.code_review.git_ops.get_diff", return_value="diff --git"),
            patch("reviewer.code_review.llm.review_diff",
                  return_value={"status": "pass", "summary": "ok", "findings": []}),
            patch("reviewer.code_review.ForgejoClient", return_value=fg),
            patch("reviewer.code_review.redis.from_url", return_value=r),
        ):
            from reviewer.code_review import run_code_review
            result = run_code_review("alice/backend", 7, "a" * 40)

        assert result["status"] == "pass"          # allowed — refactor intent

    def test_forgejo_error_does_not_block(self):
        # If fetching PR files fails, the guard must not block or crash the review.
        r = fakeredis.FakeRedis()
        fg = _make_forgejo()
        fg.get_pr_files.side_effect = RuntimeError("forgejo down")
        with (
            patch("reviewer.code_review.git_ops.clone"),
            patch("reviewer.code_review.git_ops.get_diff", return_value="diff --git"),
            patch("reviewer.code_review.llm.review_diff",
                  return_value={"status": "pass", "summary": "ok", "findings": []}),
            patch("reviewer.code_review.ForgejoClient", return_value=fg),
            patch("reviewer.code_review.redis.from_url", return_value=r),
        ):
            from reviewer.code_review import run_code_review
            result = run_code_review("alice/backend", 7, "a" * 40)

        assert result["status"] == "pass"


class TestFormatReviewComment:
    def test_renders_finding_with_inline_suggestion(self):
        from reviewer.code_review import _format_review_comment
        v = {"status": "fail", "summary": "issue found", "findings": [
            {"severity": "critical", "file": "a.py", "line": 91,
             "message": "Host header injection",
             "suggestion": "use settings.public_host instead of request headers"}]}
        out = _format_review_comment(v)
        assert "**CRITICAL** `a.py:91` — Host header injection" in out
        assert "Suggested fix: `use settings.public_host instead of request headers`" in out

    def test_renders_multiline_suggestion_as_code_block(self):
        from reviewer.code_review import _format_review_comment
        v = {"status": "fail", "summary": "x", "findings": [
            {"severity": "high", "file": "b.py", "line": 5, "message": "m",
             "suggestion": "if host not in allowed:\n    raise ValueError"}]}
        out = _format_review_comment(v)
        assert "Suggested fix:\n```\nif host not in allowed:\n    raise ValueError\n```" in out

    def test_finding_without_suggestion_still_renders(self):
        from reviewer.code_review import _format_review_comment
        v = {"status": "warn", "summary": "s", "findings": [
            {"severity": "low", "file": "c.py", "line": 1, "message": "nit"}]}
        out = _format_review_comment(v)
        assert "**LOW** `c.py:1` — nit" in out
        assert "Suggested fix" not in out
