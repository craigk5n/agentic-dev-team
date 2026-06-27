"""Tests for the test runner agent."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import fakeredis
import pytest

from reviewer.test_runner import detect_test_command, _parse_pytest_output


class TestDetectTestCommand:
    def test_detects_pytest_ini(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]")
        cmd = detect_test_command(str(tmp_path))
        assert "pytest" in cmd

    def test_detects_pyproject_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths=[\"tests\"]")
        cmd = detect_test_command(str(tmp_path))
        assert "pytest" in cmd

    def test_detects_npm_test(self, tmp_path):
        pkg = {"scripts": {"test": "jest"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        cmd = detect_test_command(str(tmp_path))
        assert "npm" in cmd

    def test_detects_makefile_test(self, tmp_path):
        (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
        cmd = detect_test_command(str(tmp_path))
        assert "make" in cmd

    def test_fallback_to_pytest(self, tmp_path):
        cmd = detect_test_command(str(tmp_path))
        assert "pytest" in cmd


class TestParsePytestOutput:
    def test_success(self):
        status, failures = _parse_pytest_output("5 passed in 0.3s", 0)
        assert status == "pass"
        assert failures == []

    def test_failure_extracts_failed_lines(self):
        output = "FAILED tests/test_main.py::test_foo - AssertionError\n2 failed in 0.5s"
        status, failures = _parse_pytest_output(output, 1)
        assert status == "fail"
        assert len(failures) == 1
        assert "test_foo" in failures[0]


class TestRunTests:
    def _make_forgejo(self):
        fg = MagicMock()
        fg.__enter__ = lambda s: s
        fg.__exit__ = MagicMock(return_value=False)
        fg.post_pr_comment.return_value = {}
        return fg

    def test_passing_tests(self, tmp_path):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "5 passed"
        proc.stderr = ""
        r = fakeredis.FakeRedis()

        with (
            patch("reviewer.test_runner.git_ops.clone"),
            patch("reviewer.test_runner.git_ops.checkout"),
            patch("reviewer.test_runner.subprocess.run", return_value=proc),
            patch("reviewer.test_runner.ForgejoClient", return_value=self._make_forgejo()),
            patch("reviewer.test_runner.redis.from_url", return_value=r),
        ):
            from reviewer.test_runner import run_tests
            result = run_tests("alice/backend", 7, "a" * 40)

        assert result["status"] == "pass"
        assert result["role"] == "test_run"

    def test_failing_tests(self, tmp_path):
        proc = MagicMock()
        proc.returncode = 1
        proc.stdout = "FAILED tests/test_foo.py::test_bar\n1 failed"
        proc.stderr = ""
        r = fakeredis.FakeRedis()

        with (
            patch("reviewer.test_runner.git_ops.clone"),
            patch("reviewer.test_runner.git_ops.checkout"),
            patch("reviewer.test_runner.subprocess.run", return_value=proc),
            patch("reviewer.test_runner.ForgejoClient", return_value=self._make_forgejo()),
            patch("reviewer.test_runner.redis.from_url", return_value=r),
        ):
            from reviewer.test_runner import run_tests
            result = run_tests("alice/backend", 7, "a" * 40)

        assert result["status"] == "fail"
