"""Code review agent: clone PR, diff against base, call LLM, post verdict."""

from __future__ import annotations
import tempfile

import redis
import structlog

from reviewer import git_ops, llm
from reviewer.config import settings
from reviewer.forgejo_client import ForgejoClient
from reviewer.verdicts import store_and_check, post_aggregated_and_gate

log = structlog.get_logger()


def _format_review_comment(verdict: dict) -> str:
    icons = {"pass": "✅", "warn": "⚠️", "fail": "❌"}
    icon = icons.get(verdict.get("status", "warn"), "❓")
    findings = verdict.get("findings", [])

    lines = [f"## {icon} Code Review\n", f"**Status:** {verdict.get('status', 'warn').capitalize()}"]
    lines.append(f"\n{verdict.get('summary', '')}\n")

    if findings:
        lines.append("### Findings\n")
        for f in findings:
            sev = f.get("severity", "low").upper()
            loc = f"{f.get('file', '?')}:{f.get('line', '?')}"
            lines.append(f"- **{sev}** `{loc}` — {f.get('message', '')}")
    else:
        lines.append("_No findings._")

    return "\n".join(lines)


def run_code_review(
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    base_ref: str = "main",
    head_ref: str = "",
    model_override: str = "",
    system_prompt: str = "",
    task_prompt: str = "",
) -> dict:
    owner, repo = repo_full_name.split("/", 1)
    model = model_override or settings.model_reviewer
    log.info("code_review_start", repo=repo_full_name, pr=pr_number, sha=head_sha[:8], model=model)

    with tempfile.TemporaryDirectory() as tmpdir:
        git_ops.clone(settings.forgejo_clone_base, owner, repo, settings.forgejo_api_token, tmpdir, branch=head_ref)
        diff = git_ops.get_diff(tmpdir, base_ref, head_sha)

    if not diff.strip():
        verdict = {"status": "pass", "summary": "No diff detected.", "findings": []}
    else:
        verdict = llm.review_diff(
            diff,
            model=model,
            api_key=settings.effective_api_key,
            system_prompt=system_prompt,
            task_prompt=task_prompt,
        )

    verdict["role"] = "code_review"

    with ForgejoClient(settings.forgejo_base_url, settings.forgejo_api_token) as forgejo:
        forgejo.post_pr_comment(owner, repo, pr_number, _format_review_comment(verdict))

    r = redis.from_url(settings.redis_url, decode_responses=False)
    all_in = store_and_check(r, repo_full_name, pr_number, "code_review", verdict, settings.verdict_ttl)
    if all_in:
        post_aggregated_and_gate(r, repo_full_name, pr_number, all_in)

    log.info("code_review_done", repo=repo_full_name, pr=pr_number, status=verdict.get("status"))
    return verdict
