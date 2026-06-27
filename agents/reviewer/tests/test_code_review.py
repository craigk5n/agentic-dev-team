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
