"""Security scan agent: run semgrep + gitleaks, post verdict."""

from __future__ import annotations
import json
import shutil
import subprocess
import tempfile

import redis
import structlog

from reviewer import git_ops
from reviewer.config import settings
from reviewer.forgejo_client import ForgejoClient
from reviewer.verdicts import store_and_check, post_aggregated_and_gate

log = structlog.get_logger()

_SCAN_TIMEOUT = 180  # seconds


def _run_semgrep(repo_dir: str) -> tuple[list[dict], str | None]:
    """Returns (findings, error_message). findings is a list of semgrep result objects."""
    if not shutil.which("semgrep"):
        log.warning("semgrep_not_installed")
        return [], "semgrep not installed — skipped"

    result = subprocess.run(
        ["semgrep", "scan", "--json", "--config", "auto", "--no-rewrite-rule-ids", "."],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=_SCAN_TIMEOUT,
    )
    # semgrep: 0=ok, 1=findings, 2=error, 3=timeout
    if result.returncode > 1:
        return [], f"semgrep error: {result.stderr[:300]}"
    try:
        data = json.loads(result.stdout)
        return data.get("results", []), None
    except json.JSONDecodeError:
        return [], "semgrep returned non-JSON output"


def _run_gitleaks(repo_dir: str) -> tuple[list[dict], str | None]:
    """Returns (findings, error_message)."""
    if not shutil.which("gitleaks"):
        log.warning("gitleaks_not_installed")
        return [], "gitleaks not installed — skipped"

    out_path = f"{repo_dir}/.gitleaks-report.json"
    result = subprocess.run(
        [
            "gitleaks", "detect",
            "--source", ".",
            "--report-format", "json",
            "--report-path", out_path,
            "--no-git",
        ],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=_SCAN_TIMEOUT,
    )
    # gitleaks: 0=no leaks, 1=leaks found, 126=config error
    if result.returncode > 1:
        return [], f"gitleaks error: {result.stderr[:300]}"
    try:
        import os
        if not os.path.exists(out_path):
            return [], None
        with open(out_path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else [], None
    except (json.JSONDecodeError, OSError):
        return [], "gitleaks returned unreadable output"


def _semgrep_severity(finding: dict) -> str:
    """Map semgrep severity string to our 4-tier scheme."""
    raw = finding.get("extra", {}).get("severity", "INFO").upper()
    return {"ERROR": "high", "WARNING": "medium", "INFO": "low"}.get(raw, "low")


def _format_security_comment(semgrep_findings: list, secret_findings: list, errors: list[str]) -> tuple[str, str]:
    """Returns (markdown_comment, status)."""
    icons = {"high": "🔴", "medium": "🟡", "low": "🔵", "critical": "🔴"}
    status = "pass"
    lines = ["## 🔐 Security Scan\n"]

    if errors:
        for e in errors:
            lines.append(f"> ℹ️ {e}")
        lines.append("")

    # Semgrep
    if semgrep_findings:
        status = "fail" if any(_semgrep_severity(f) == "high" for f in semgrep_findings) else "warn"
        lines.append(f"### Semgrep ({len(semgrep_findings)} finding{'s' if len(semgrep_findings) != 1 else ''})\n")
        for f in semgrep_findings[:20]:
            sev = _semgrep_severity(f)
            path = f.get("path", "?")
            line = f.get("start", {}).get("line", "?")
            msg = f.get("extra", {}).get("message", "")[:120]
            lines.append(f"- {icons.get(sev, '•')} `{path}:{line}` — {msg}")
    else:
        lines.append("### Semgrep\n_No findings._")

    lines.append("")

    # Secret scan
    if secret_findings:
        status = "fail"
        lines.append(f"### Secret Scan ({len(secret_findings)} potential secret{'s' if len(secret_findings) != 1 else ''})\n")
        for s in secret_findings[:10]:
            desc = s.get("Description", "secret")
            path = s.get("File", "?")
            line = s.get("StartLine", "?")
            lines.append(f"- 🔴 `{path}:{line}` — {desc}")
    else:
        lines.append("### Secret Scan\n_No secrets detected._")

    return "\n".join(lines), status


def run_security_scan(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    base_ref: str = "main",
    head_ref: str = "",
    model_override: str = "",  # reserved for future LLM-assisted triage
) -> dict:
    owner, repo = repo_full_name.split("/", 1)
    log.info("security_scan_start", repo=repo_full_name, pr=pr_number, sha=head_sha[:8])

    with tempfile.TemporaryDirectory() as tmpdir:
        git_ops.clone(settings.forgejo_clone_base, owner, repo, settings.forgejo_api_token, tmpdir, branch=head_ref)
        git_ops.checkout(tmpdir, head_sha)

        semgrep_findings, semgrep_err = _run_semgrep(tmpdir)
        secret_findings, gitleaks_err = _run_gitleaks(tmpdir)

    errors = [e for e in (semgrep_err, gitleaks_err) if e]
    comment, status = _format_security_comment(semgrep_findings, secret_findings, errors)

    total = len(semgrep_findings) + len(secret_findings)
    summary = (
        f"No security issues found." if total == 0
        else f"{total} finding{'s' if total != 1 else ''} detected."
    )
    verdict = {
        "role": "security",
        "status": status,
        "summary": summary,
        "semgrep_count": len(semgrep_findings),
        "secret_count": len(secret_findings),
    }

    with ForgejoClient(settings.forgejo_base_url, settings.forgejo_api_token) as forgejo:
        forgejo.post_pr_comment(owner, repo, pr_number, comment)

    r = redis.from_url(settings.redis_url, decode_responses=False)

    # Record a scan call in telemetry (cost=0, no LLM used — static analysis only)
    try:
        import time
        date = time.strftime("%Y-%m-%d")
        key = f"telemetry:llm:{date}"
        r.hincrby(key, "security::calls", 1)
        r.expire(key, 30 * 86_400)
    except Exception:
        pass

    all_in = store_and_check(r, repo_full_name, pr_number, "security", verdict, settings.verdict_ttl)
    if all_in:
        post_aggregated_and_gate(r, repo_full_name, pr_number, all_in)

    log.info("security_scan_done", repo=repo_full_name, pr=pr_number, status=status)
    return verdict
