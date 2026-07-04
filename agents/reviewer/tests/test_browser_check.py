"""Tests for the HS-1 headless-browser E2E check.

These cover the pure decision logic (which changes are UI, how a run command is derived,
which console messages are blocking, how skips are decided, how the result merges into the
test verdict). The live-browser path (run_browser_check driving Chromium) is validated
manually against a running app — here we only assert it degrades to ``skip`` when a browser
or launch command is unavailable, which is the branch that runs in CI.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from reviewer.browser_check import (
    is_ui_change,
    discover_run_command,
    _classify_console,
    browser_verdict,
)
from reviewer.test_runner import _merge_browser_result


class TestIsUiChange:
    def test_html_template_is_ui(self):
        assert is_ui_change(["app/templates/index.html"]) is True

    def test_static_asset_is_ui(self):
        assert is_ui_change(["src/static/app.js"]) is True
        assert is_ui_change(["public/styles.css"]) is True

    def test_jinja_and_frontend_exts(self):
        assert is_ui_change(["ui/page.jinja"]) is True
        assert is_ui_change(["web/Widget.tsx"]) is True
        assert is_ui_change(["web/Widget.vue"]) is True

    def test_ui_route_segment(self):
        assert is_ui_change(["app/routes/ui_admin.py"]) is True

    def test_backend_only_is_not_ui(self):
        assert is_ui_change(["src/registry/store.py", "README.md"]) is False

    def test_empty_and_none(self):
        assert is_ui_change([]) is False
        assert is_ui_change(None) is False


class TestDiscoverRunCommand:
    def test_explicit_wins(self, tmp_path: Path):
        assert discover_run_command(str(tmp_path), "uvicorn app:app") == "uvicorn app:app"

    def test_explicit_stripped(self, tmp_path: Path):
        assert discover_run_command(str(tmp_path), "  run-me  ") == "run-me"

    def test_derives_from_project_scripts(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""
            [project]
            name = "devhub"

            [project.scripts]
            devhub-server = "devhub.main:run"
        """))
        cmd = discover_run_command(str(tmp_path))
        assert "devhub-server" in cmd
        assert "$PORT" in cmd  # left for the caller to substitute

    def test_no_pyproject_no_command(self, tmp_path: Path):
        assert discover_run_command(str(tmp_path)) == ""

    def test_pyproject_without_scripts(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        assert discover_run_command(str(tmp_path)) == ""


class TestClassifyConsole:
    def test_blocking_markers_are_kept(self):
        errs = [
            "Failed to find a valid digest in the 'integrity' attribute",
            "Refused to execute script because its MIME type",
            "Uncaught ReferenceError: htmx is not defined",
            "pageerror: TypeError: x is not a function",
        ]
        assert len(_classify_console(errs)) == 4

    def test_favicon_is_ignored(self):
        assert _classify_console(["GET /favicon.ico 404 (Not Found)"]) == []

    def test_cosmetic_noise_is_ignored(self):
        # A plain 404 log line with no hard-error marker is not blocking.
        assert _classify_console(["some informational message"]) == []

    def test_mixed(self):
        hard = _classify_console([
            "favicon.ico 404",
            "Uncaught SyntaxError: Unexpected token",
        ])
        assert len(hard) == 1
        assert "SyntaxError" in hard[0]


class TestBrowserVerdictSkips:
    def test_skip_when_not_ui(self, tmp_path: Path):
        res = browser_verdict(str(tmp_path), ["src/store.py"], run_command="run-me")
        assert res["status"] == "skip"
        assert "UI" in res["reason"] or "static" in res["reason"]

    def test_skip_when_no_run_command(self, tmp_path: Path):
        # UI change but nothing to launch and no pyproject to derive from.
        res = browser_verdict(str(tmp_path), ["templates/x.html"], run_command="")
        assert res["status"] == "skip"
        assert "run_command" in res["reason"]

    def test_skip_when_app_never_starts(self, tmp_path: Path):
        # UI change + a launch command that exits immediately → no port ever opens → skip.
        res = browser_verdict(
            str(tmp_path), ["templates/x.html"],
            run_command="true", port=8611, startup_timeout=1.5,
        )
        assert res["status"] == "skip"
        assert "did not start" in res["reason"]

    def test_never_raises(self, tmp_path: Path):
        # Garbage command must not blow up the verdict.
        res = browser_verdict(
            str(tmp_path), ["templates/x.html"],
            run_command="this-binary-does-not-exist-xyz", port=8612, startup_timeout=1.5,
        )
        assert res["status"] == "skip"


class TestMergeBrowserResult:
    def test_browser_fail_forces_fail(self):
        status, failures, summary = _merge_browser_result(
            "pass", [], "Tests passed.",
            {"status": "fail", "summary": "1 blocking console error(s): Uncaught ..."},
        )
        assert status == "fail"
        assert any("Browser E2E FAILED" in f for f in failures)
        assert "Browser E2E FAILED" in summary

    def test_browser_pass_keeps_status_and_notes(self):
        status, failures, summary = _merge_browser_result(
            "pass", [], "Tests passed.",
            {"status": "pass", "summary": "8 control(s) fired and rendered visibly"},
        )
        assert status == "pass"
        assert "Browser E2E:" in summary

    def test_browser_skip_is_noted_never_silent(self):
        status, failures, summary = _merge_browser_result(
            "pass", [], "Tests passed.",
            {"status": "skip", "reason": "no UI/template/static change in this PR"},
        )
        assert status == "pass"
        assert "skipped" in summary

    def test_browser_fail_does_not_drop_existing_failures(self):
        status, failures, summary = _merge_browser_result(
            "fail", ["test_x FAILED"], "Tests failed.",
            {"status": "fail", "summary": "control result stayed hidden"},
        )
        assert status == "fail"
        assert "test_x FAILED" in failures
        assert any("Browser E2E FAILED" in f for f in failures)
