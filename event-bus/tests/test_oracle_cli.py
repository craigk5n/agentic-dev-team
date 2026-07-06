"""Story 2.2 — CLI golden-file oracle backend."""
from __future__ import annotations

import os
import stat

import pytest

from event_bus import oracle
from event_bus.oracle.backends.cli import cli_backend


def _script(tmp_path, body: str):
    """Write an executable /bin/sh script and return its path."""
    p = tmp_path / "app.sh"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return str(p)


class TestCliBackend:
    def test_passes_on_matching_stdout_and_exit(self, tmp_path):
        binary = _script(tmp_path, 'echo "hello $1"\n')
        spec = {"name": "greet", "backend": "cli", "checks": [
            {"name": "greets", "argv": ["world"], "expect_stdout": "hello world\n"}]}
        checks, defects = cli_backend(spec, {"binary": binary})
        assert checks[0]["passed"] is True and defects == []

    def test_stdout_mismatch_is_structured_defect(self, tmp_path):
        binary = _script(tmp_path, 'echo "actual"\n')
        spec = {"name": "x", "backend": "cli", "checks": [
            {"name": "chk", "expect_stdout": "expected\n"}]}
        checks, defects = cli_backend(spec, {"binary": binary})
        assert checks[0]["passed"] is False
        d = defects[0]
        assert d["class"] == "stdout-mismatch" and d["check"] == "chk"
        assert d["expected"] == "expected\n" and d["actual"] == "actual\n"

    def test_exit_code_mismatch_defect(self, tmp_path):
        binary = _script(tmp_path, "exit 3\n")
        spec = {"name": "x", "backend": "cli", "checks": [
            {"name": "chk", "expect_exit": 0}]}
        _, defects = cli_backend(spec, {"binary": binary})
        assert defects[0]["class"] == "exit-code" and defects[0]["actual"] == 3

    def test_output_file_check(self, tmp_path):
        binary = _script(tmp_path, 'printf "content" > out.txt\n')
        spec = {"name": "x", "backend": "cli", "checks": [
            {"name": "writes", "expect_files": {"out.txt": "content"}}]}
        checks, defects = cli_backend(spec, {"binary": binary, "workdir": str(tmp_path)})
        assert checks[0]["passed"] is True and defects == []

    def test_output_file_mismatch_defect(self, tmp_path):
        binary = _script(tmp_path, 'printf "wrong" > out.txt\n')
        spec = {"name": "x", "backend": "cli", "checks": [
            {"name": "writes", "expect_files": {"out.txt": "right"}}]}
        _, defects = cli_backend(spec, {"binary": binary, "workdir": str(tmp_path)})
        assert defects[0]["class"] == "file-mismatch" and defects[0]["file"] == "out.txt"

    def test_missing_binary_is_defect_not_crash(self, tmp_path):
        spec = {"name": "x", "backend": "cli", "checks": [{"name": "chk"}]}
        checks, defects = cli_backend(spec, {"binary": str(tmp_path / "nope")})
        assert checks[0]["passed"] is False
        assert defects[0]["class"] == "missing-binary"

    def test_no_binary_in_target_is_defect(self):
        spec = {"name": "x", "backend": "cli", "checks": [{"name": "chk"}]}
        checks, defects = cli_backend(spec, {})
        assert defects[0]["class"] == "missing-binary"

    def test_deterministic_across_runs(self, tmp_path):
        binary = _script(tmp_path, 'echo "stable"\n')
        spec = {"name": "x", "backend": "cli", "checks": [
            {"name": "chk", "expect_stdout": "stable\n"}]}
        r1 = cli_backend(spec, {"binary": binary})
        r2 = cli_backend(spec, {"binary": binary})
        assert r1 == r2

    def test_registered_as_default_backend_via_run_oracle(self, tmp_path):
        # The default "cli" backend is wired into run_oracle by the oracle package.
        binary = _script(tmp_path, 'echo "ok"\n')
        spec = {"name": "e2e", "backend": "cli", "checks": [
            {"name": "chk", "expect_stdout": "ok\n"}]}
        res = oracle.run_oracle(spec, {"binary": binary})
        assert res["passed"] is True and res["backend"] == "cli"
