"""Tests for reviewer/gate.py — merge gate logic."""

from __future__ import annotations
import json
from unittest.mock import MagicMock, patch, call

import fakeredis
import pytest


def _make_redis(gates: dict | None = None) -> fakeredis.FakeRedis:
    r = fakeredis.FakeRedis()
    if gates is not None:
        r.set("runtime_config", json.dumps({"gates": gates}))
    return r


def _verdicts(security_status: str = "pass") -> dict:
    return {
        "code_review": {"status": "pass", "summary": "ok"},
        "test_run": {"status": "pass", "summary": "ok"},
        "security": {"status": security_status, "summary": "scan done"},
    }


class TestApplyGate:
    def test_auto_merge_when_no_gates_blocking(self):
        r = _make_redis({"security_signoff": False, "pr_merge_approval": False})
        with patch("reviewer.gate.ForgejoClient") as mock_fj:
            ctx = MagicMock()
            ctx.merge_pr.return_value = {"merged": True}
            mock_fj.return_value.__enter__.return_value = ctx
            from reviewer.gate import apply_gate
            result = apply_gate(r, "owner/repo", 1, _verdicts("pass"))
        assert result["gate_status"] == "merged"
        ctx.merge_pr.assert_called_once_with("owner", "repo", 1)

    def test_security_signoff_blocks_on_fail(self):
        r = _make_redis({"security_signoff": True, "pr_merge_approval": False})
        with patch("reviewer.gate.ForgejoClient") as mock_fj:
            ctx = MagicMock()
            mock_fj.return_value.__enter__.return_value = ctx
            from reviewer.gate import apply_gate
            result = apply_gate(r, "owner/repo", 1, _verdicts("fail"))
        assert result["gate_status"] == "blocked"
        assert result["reason"] == "security_fail"
        ctx.post_pr_comment.assert_called_once()
        ctx.merge_pr.assert_not_called()

    def test_security_signoff_passes_on_warn(self):
        # security_signoff only blocks on 'fail', not 'warn'
        r = _make_redis({"security_signoff": True, "pr_merge_approval": False})
        with patch("reviewer.gate.ForgejoClient") as mock_fj:
            ctx = MagicMock()
            ctx.merge_pr.return_value = {"merged": True}
            mock_fj.return_value.__enter__.return_value = ctx
            from reviewer.gate import apply_gate
            result = apply_gate(r, "owner/repo", 1, _verdicts("warn"))
        assert result["gate_status"] == "merged"

    def test_pr_merge_approval_stores_pending_and_posts_comment(self):
        r = _make_redis({"security_signoff": False, "pr_merge_approval": True})
        with patch("reviewer.gate.ForgejoClient") as mock_fj:
            ctx = MagicMock()
            mock_fj.return_value.__enter__.return_value = ctx
            from reviewer.gate import apply_gate
            result = apply_gate(r, "owner/repo", 1, _verdicts("pass"))
        assert result["gate_status"] == "awaiting_approval"
        # Pending merge token must be stored in Redis
        assert r.exists("pr_merge_pending:owner:repo:1")
        # Comment explaining how to approve should be posted
        ctx.post_pr_comment.assert_called_once()
        comment_body = ctx.post_pr_comment.call_args[0][3]
        assert "/api/prs/owner/repo/1/approve" in comment_body

    def test_pr_merge_approval_takes_priority_over_security_warn(self):
        # security warn + pr_merge_approval → hold for approval, not auto-merge
        r = _make_redis({"security_signoff": True, "pr_merge_approval": True})
        with patch("reviewer.gate.ForgejoClient") as mock_fj:
            ctx = MagicMock()
            mock_fj.return_value.__enter__.return_value = ctx
            from reviewer.gate import apply_gate
            result = apply_gate(r, "owner/repo", 1, _verdicts("warn"))
        # security warns pass signoff, then hits approval gate
        assert result["gate_status"] == "awaiting_approval"

    def test_security_fail_blocks_even_when_pr_merge_approval_on(self):
        r = _make_redis({"security_signoff": True, "pr_merge_approval": True})
        with patch("reviewer.gate.ForgejoClient") as mock_fj:
            ctx = MagicMock()
            mock_fj.return_value.__enter__.return_value = ctx
            from reviewer.gate import apply_gate
            result = apply_gate(r, "owner/repo", 1, _verdicts("fail"))
        assert result["gate_status"] == "blocked"

    def test_default_gates_block_merge_on_security_fail(self):
        # Default: security_signoff=True — no config in Redis
        r = _make_redis()
        with patch("reviewer.gate.ForgejoClient") as mock_fj:
            ctx = MagicMock()
            mock_fj.return_value.__enter__.return_value = ctx
            from reviewer.gate import apply_gate
            result = apply_gate(r, "owner/repo", 1, _verdicts("fail"))
        assert result["gate_status"] == "blocked"

    def test_default_gates_auto_merge_on_all_pass(self):
        # Default: pr_merge_approval=False → auto-merge when all pass
        r = _make_redis()
        with patch("reviewer.gate.ForgejoClient") as mock_fj:
            ctx = MagicMock()
            ctx.merge_pr.return_value = {"merged": True}
            mock_fj.return_value.__enter__.return_value = ctx
            from reviewer.gate import apply_gate
            result = apply_gate(r, "owner/repo", 1, _verdicts("pass"))
        assert result["gate_status"] == "merged"

    def test_merge_error_returns_error_gate_status(self):
        r = _make_redis({"security_signoff": False, "pr_merge_approval": False})
        with patch("reviewer.gate.ForgejoClient") as mock_fj:
            ctx = MagicMock()
            ctx.merge_pr.side_effect = RuntimeError("409 Conflict")
            mock_fj.return_value.__enter__.return_value = ctx
            from reviewer.gate import apply_gate
            result = apply_gate(r, "owner/repo", 1, _verdicts("pass"))
        assert result["gate_status"] == "merge_error"
        assert "409" in result["error"]

    def test_missing_security_verdict_treated_as_warn(self):
        # If security verdict is absent (only code_review + test_run arrived), status defaults to 'warn'
        r = _make_redis({"security_signoff": True, "pr_merge_approval": False})
        verdicts = {"code_review": {"status": "pass"}, "test_run": {"status": "pass"}}
        with patch("reviewer.gate.ForgejoClient") as mock_fj:
            ctx = MagicMock()
            ctx.merge_pr.return_value = {"merged": True}
            mock_fj.return_value.__enter__.return_value = ctx
            from reviewer.gate import apply_gate
            result = apply_gate(r, "owner/repo", 1, verdicts)
        # 'warn' does not trigger security_signoff block → should auto-merge
        assert result["gate_status"] == "merged"
