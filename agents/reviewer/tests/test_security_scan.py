"""Tests for the security scan agent."""

import json
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from reviewer.security_scan import (
    _semgrep_severity,
    _format_security_comment,
    _classify_blocking_categories,
)


def _semgrep(check_id="", message="", metadata=None):
    return {"check_id": check_id,
            "extra": {"message": message, "metadata": metadata or {}}}


class TestClassifyBlockingCategories:
    def test_detects_missing_sri(self):
        f = _semgrep(check_id="html.security.audit.missing-integrity.missing-integrity",
                     message="This tag is missing an 'integrity' subresource attribute")
        assert _classify_blocking_categories([f], []) == ["missing-sri"]

    def test_detects_template_xss(self):
        f = _semgrep(check_id="python.flask.security.xss.audit.template-autoescape-off",
                     message="Detected a potential XSS via mark_safe")
        assert _classify_blocking_categories([f], []) == ["template-xss"]

    def test_gitleaks_secret_is_hardcoded_secret(self):
        assert _classify_blocking_categories([], [{"Description": "AWS key"}]) == ["hardcoded-secret"]

    def test_semgrep_hardcoded_secret_marker(self):
        f = _semgrep(check_id="generic.secrets.security.detected-generic-api-key",
                     message="Hardcoded API key detected")
        assert _classify_blocking_categories([f], []) == ["hardcoded-secret"]

    def test_benign_finding_no_categories(self):
        f = _semgrep(check_id="python.lang.best-practice.unused-import",
                     message="Unused import")
        assert _classify_blocking_categories([f], []) == []

    def test_multiple_sorted_and_deduped(self):
        f1 = _semgrep(check_id="a.missing-integrity.b", message="integrity")
        f2 = _semgrep(check_id="c.xss.d", message="cross-site-scripting")
        f3 = _semgrep(check_id="e.missing-integrity.f", message="subresource integrity")
        cats = _classify_blocking_categories([f1, f2, f3], [])
        assert cats == ["missing-sri", "template-xss"]


class TestSemgrepSeverity:
    def test_error_maps_to_high(self):
        assert _semgrep_severity({"extra": {"severity": "ERROR"}}) == "high"

    def test_warning_maps_to_medium(self):
        assert _semgrep_severity({"extra": {"severity": "WARNING"}}) == "medium"

    def test_info_maps_to_low(self):
        assert _semgrep_severity({"extra": {"severity": "INFO"}}) == "low"

    def test_unknown_maps_to_low(self):
        assert _semgrep_severity({}) == "low"


class TestFormatSecurityComment:
    def test_clean_pass(self):
        comment, status = _format_security_comment([], [], [])
        assert status == "pass"
        assert "No findings" in comment

    def test_semgrep_high_is_fail(self):
        finding = {
            "path": "src/db.py",
            "start": {"line": 10},
            "extra": {"severity": "ERROR", "message": "SQL injection risk"},
        }
        comment, status = _format_security_comment([finding], [], [])
        assert status == "fail"
        assert "src/db.py" in comment

    def test_semgrep_medium_is_warn(self):
        finding = {
            "path": "src/api.py",
            "start": {"line": 5},
            "extra": {"severity": "WARNING", "message": "weak crypto"},
        }
        comment, status = _format_security_comment([finding], [], [])
        assert status == "warn"

    def test_secrets_are_fail(self):
        secret = {"Description": "Generic API Key", "File": "config.py", "StartLine": 3}
        comment, status = _format_security_comment([], [secret], [])
        assert status == "fail"
        assert "config.py" in comment

    def test_errors_shown_in_comment(self):
        comment, status = _format_security_comment([], [], ["semgrep not installed — skipped"])
        assert "semgrep not installed" in comment


class TestRunSecurityScan:
    def _make_forgejo(self):
        fg = MagicMock()
        fg.__enter__ = lambda s: s
        fg.__exit__ = MagicMock(return_value=False)
        fg.post_pr_comment.return_value = {}
        return fg

    def test_clean_scan_posts_pass(self):
        r = fakeredis.FakeRedis()
        with (
            patch("reviewer.security_scan.git_ops.clone"),
            patch("reviewer.security_scan.git_ops.checkout"),
            patch("reviewer.security_scan._run_semgrep", return_value=([], None)),
            patch("reviewer.security_scan._run_gitleaks", return_value=([], None)),
            patch("reviewer.security_scan.ForgejoClient", return_value=self._make_forgejo()),
            patch("reviewer.security_scan.redis.from_url", return_value=r),
        ):
            from reviewer.security_scan import run_security_scan
            result = run_security_scan("alice/backend", 7, "a" * 40)

        assert result["status"] == "pass"
        assert result["role"] == "security"

    def test_findings_set_fail(self):
        r = fakeredis.FakeRedis()
        finding = {"path": "x.py", "start": {"line": 1}, "extra": {"severity": "ERROR", "message": "bad"}}
        with (
            patch("reviewer.security_scan.git_ops.clone"),
            patch("reviewer.security_scan.git_ops.checkout"),
            patch("reviewer.security_scan._run_semgrep", return_value=([finding], None)),
            patch("reviewer.security_scan._run_gitleaks", return_value=([], None)),
            patch("reviewer.security_scan.ForgejoClient", return_value=self._make_forgejo()),
            patch("reviewer.security_scan.redis.from_url", return_value=r),
        ):
            from reviewer.security_scan import run_security_scan
            result = run_security_scan("alice/backend", 7, "a" * 40)

        assert result["status"] == "fail"
        assert result["semgrep_count"] == 1
