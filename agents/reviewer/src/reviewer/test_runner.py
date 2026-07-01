"""Test runner agent: clone PR branch, detect test command, run tests, post verdict."""

from __future__ import annotations
import json
import subprocess
import tempfile
from pathlib import Path

import redis
import structlog

from reviewer import git_ops, llm
from reviewer.config import settings
from reviewer.forgejo_client import ForgejoClient
from reviewer.verdicts import store_and_check, post_aggregated_and_gate

log = structlog.get_logger()

_TEST_TIMEOUT = 300  # seconds


def detect_test_command(repo_dir: str, stack: str = "") -> list[str]:
    p = Path(repo_dir)

    # An explicit stack hint takes priority over file sniffing: a Go or Node repo
    # must NOT fall through to the `python -m pytest` fallback (which collects zero
    # tests and reports a misleading failure). The toolchain may be absent from the
    # triage sandbox — run_tests degrades that to an advisory `warn` (CI is the
    # authoritative per-stack test gate).
    if stack == "go":
        return ["go", "test", "./..."]
    if stack == "node-ts":
        return ["npm", "test", "--silent"]

    if (p / "pytest.ini").exists():
        return ["python", "-m", "pytest", "--tb=short", "-q"]
    if (p / "pyproject.toml").exists():
        content = (p / "pyproject.toml").read_text()
        if "[tool.pytest" in content:
            return ["python", "-m", "pytest", "--tb=short", "-q"]
    if (p / "package.json").exists():
        try:
            pkg = json.loads((p / "package.json").read_text())
            if "test" in pkg.get("scripts", {}):
                return ["npm", "test", "--", "--ci", "--passWithNoTests"]
        except json.JSONDecodeError:
            pass
    if (p / "Makefile").exists():
        content = (p / "Makefile").read_text()
        if "test:" in content or "\ntest :" in content:
            return ["make", "test"]
    # Fallback
    return ["python", "-m", "pytest", "--tb=short", "-q"]


def _try_install_deps(repo_dir: str) -> None:
    p = Path(repo_dir)
    if (p / "requirements.txt").exists():
        subprocess.run(
            ["pip", "install", "-r", "requirements.txt", "--quiet"],
            cwd=repo_dir, capture_output=True, timeout=120,
        )
    if (p / "pyproject.toml").exists() or (p / "setup.py").exists():
        # Install with all extras so test dependencies (e.g. pytest) are included
        subprocess.run(
            ["pip", "install", "-e", ".[test,dev]", "--quiet"],
            cwd=repo_dir, capture_output=True, timeout=120,
        )
        # Fallback: plain install if extras aren't defined
        subprocess.run(
            ["pip", "install", "-e", ".", "--quiet"],
            cwd=repo_dir, capture_output=True, timeout=120,
        )
    # Always ensure pytest is available regardless of project structure
    subprocess.run(
        ["pip", "install", "pytest", "--quiet"],
        capture_output=True, timeout=60,
    )


def _parse_pytest_output(output: str, returncode: int) -> tuple[str, list[str]]:
    """Quick parse of pytest summary line. Falls back to LLM for complex output."""
    failures: list[str] = []
    for line in output.splitlines():
        if "FAILED" in line:
            failures.append(line.strip())
    if returncode == 0:
        return "pass", []
    return "fail", failures[:20]


def _format_test_comment(verdict: dict) -> str:
    icons = {"pass": "✅", "fail": "❌", "warn": "⚠️"}
    icon = icons.get(verdict.get("status", "fail"), "❓")
    failures = verdict.get("failures", [])
    lines = [
        f"## {icon} Test Results\n",
        f"**Status:** {verdict.get('status', 'fail').capitalize()}\n",
        verdict.get("summary", ""),
    ]
    if failures:
        lines.append("\n### Failures\n")
        for f in failures[:10]:
            lines.append(f"- `{f}`")
    return "\n".join(lines)


def _execute_tests(
    owner: str,
    repo: str,
    head_sha: str,
    head_ref: str,
    model: str,
    stack: str,
) -> tuple[str, list[str], str]:
    """Clone + run the suite, returning (status, failures, summary).

    Never raises: any environment failure (missing toolchain, clone error, timeout)
    is converted into an advisory verdict so the aggregation gate always completes.
    A non-blocking ``warn`` defers to CI, which runs the authoritative per-stack
    suite in the correct image; only a real, parsed test failure returns ``fail``.
    """
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            git_ops.clone(settings.forgejo_clone_base, owner, repo, settings.effective_forgejo_token, tmpdir, branch=head_ref)
            git_ops.checkout(tmpdir, head_sha)
            _try_install_deps(tmpdir)

            cmd = detect_test_command(tmpdir, stack=stack)
            try:
                result = subprocess.run(
                    cmd, cwd=tmpdir, capture_output=True, text=True, timeout=_TEST_TIMEOUT,
                )
            except FileNotFoundError:
                # The test runtime (e.g. `npm`, `go`) is not in the triage sandbox.
                log.warning("test_runtime_unavailable", cmd=cmd[0], stack=stack)
                return (
                    "warn", [],
                    f"Test runtime `{cmd[0]}` is unavailable in the triage sandbox; "
                    "CI runs the authoritative test suite for this stack.",
                )
            except subprocess.TimeoutExpired:
                log.warning("test_timeout", stack=stack, timeout=_TEST_TIMEOUT)
                return ("fail", [], f"Tests exceeded the {_TEST_TIMEOUT}s timeout.")
            combined = result.stdout + result.stderr
            returncode = result.returncode
    except Exception as exc:  # noqa: BLE001 — never wedge the gate on triage errors
        log.warning("test_runner_setup_failed", error=str(exc)[:200])
        return (
            "warn", [],
            f"Tests could not be executed in the triage sandbox ({str(exc)[:160]}); "
            "CI runs the authoritative test suite.",
        )

    status, failures = _parse_pytest_output(combined, returncode)

    # For complex output or many failures, ask the LLM for a clean summary
    if returncode != 0 and not failures:
        try:
            llm_result = llm.summarise_test_output(
                combined,
                model=model,
                api_key=settings.effective_api_key,
                stack=stack,
            )
            status = llm_result.get("status", "fail")
            failures = llm_result.get("failures", [])
            summary = llm_result.get("summary", "Tests failed.")
        except Exception as exc:
            log.warning("test_llm_summary_failed", error=str(exc)[:120])
            summary = f"Tests failed (LLM summary unavailable): {combined[:200]}"
    else:
        summary = f"Tests {'passed' if status == 'pass' else 'failed'}."

    return (status, failures, summary)


def run_tests(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    base_ref: str = "main",
    head_ref: str = "",
    model_override: str = "",
    stack: str = "",
) -> dict:
    owner, repo = repo_full_name.split("/", 1)
    model = model_override or settings.model_tester
    log.info("test_runner_start", repo=repo_full_name, pr=pr_number, sha=head_sha[:8], model=model)

    status, failures, summary = _execute_tests(
        owner, repo, head_sha, head_ref, model, stack,
    )

    verdict = {"role": "test_run", "status": status, "summary": summary, "failures": failures}

    with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as forgejo:
        forgejo.post_pr_comment(owner, repo, pr_number, _format_test_comment(verdict))

    r = redis.from_url(settings.redis_url, decode_responses=False)
    all_in = store_and_check(r, repo_full_name, pr_number, "test_run", verdict, settings.verdict_ttl)
    if all_in:
        post_aggregated_and_gate(r, repo_full_name, pr_number, all_in)

    log.info("test_runner_done", repo=repo_full_name, pr=pr_number, status=status)
    return verdict
