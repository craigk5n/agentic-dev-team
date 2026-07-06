"""CLI golden-file oracle backend (EPIC 2, Story 2.2).

Runs a built CLI against fixture inputs and diffs stdout / exit code / output files against
goldens declared in the spec. Every mismatch becomes a structured defect. Deterministic:
a pure function of the artifact + fixtures (no time/random).

target = {"binary": "/path/to/cli", "workdir": "/optional/cwd"}
each spec check =
    {
      "name": "help-shows-usage",
      "argv": ["--help"],                    # appended after binary
      "stdin": "optional input",
      "expect_exit": 0,                        # default 0
      "expect_stdout": "exact golden",         # optional (exact match)
      "expect_stdout_contains": "substr",      # optional (substring)
      "expect_files": {"out.txt": "golden"}    # optional (relpath -> content)
    }
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _check_one(binary: str, check: dict, workdir: str | None) -> tuple[bool, str, dict | None]:
    name = check.get("name")
    argv = [binary, *[str(a) for a in check.get("argv", [])]]
    try:
        proc = subprocess.run(argv, input=check.get("stdin"), capture_output=True,
                              text=True, cwd=workdir, timeout=check.get("timeout", 60))
    except FileNotFoundError:
        return False, "binary not found", {
            "class": "missing-binary", "check": name,
            "description": f"cannot execute {binary!r}"}
    except subprocess.TimeoutExpired:
        return False, "timed out", {
            "class": "timeout", "check": name,
            "description": f"{name} exceeded timeout"}

    expect_exit = check.get("expect_exit", 0)
    if proc.returncode != expect_exit:
        return False, f"exit {proc.returncode} != {expect_exit}", {
            "class": "exit-code", "check": name, "expected": expect_exit,
            "actual": proc.returncode,
            "description": f"expected exit {expect_exit}, got {proc.returncode}"}

    if "expect_stdout" in check and proc.stdout != check["expect_stdout"]:
        return False, "stdout mismatch", {
            "class": "stdout-mismatch", "check": name,
            "expected": check["expect_stdout"], "actual": proc.stdout,
            "description": "stdout does not match golden"}

    sub = check.get("expect_stdout_contains")
    if sub is not None and sub not in proc.stdout:
        return False, "stdout missing expected substring", {
            "class": "stdout-missing", "check": name, "expected": sub,
            "actual": proc.stdout, "description": f"stdout lacks {sub!r}"}

    for rel, expected in (check.get("expect_files") or {}).items():
        fp = Path(workdir or ".") / rel
        actual = fp.read_text() if fp.is_file() else None
        if actual != expected:
            return False, f"file {rel} mismatch", {
                "class": "file-mismatch", "check": name, "file": rel,
                "expected": expected, "actual": actual,
                "description": f"output file {rel} does not match golden"}

    return True, "ok", None


def cli_backend(spec: dict, target: dict) -> tuple[list[dict], list[dict]]:
    """Run all CLI checks; return (checks, defects)."""
    binary = (target or {}).get("binary")
    workdir = (target or {}).get("workdir")
    checks_out: list[dict] = []
    defects: list[dict] = []
    for check in spec.get("checks", []):
        name = check.get("name")
        if not binary:
            checks_out.append({"name": name, "passed": False, "detail": "no binary provided"})
            defects.append({"class": "missing-binary", "check": name,
                            "description": "no binary in target"})
            continue
        passed, detail, defect = _check_one(binary, check, workdir)
        checks_out.append({"name": name, "passed": passed, "detail": detail})
        if defect:
            defects.append(defect)
    return checks_out, defects
