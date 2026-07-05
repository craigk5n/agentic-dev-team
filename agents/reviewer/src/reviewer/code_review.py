"""Code review agent: clone PR, diff against base, call LLM, post verdict."""

from __future__ import annotations
import tempfile

import redis
import structlog

from reviewer import git_ops, llm
from reviewer.config import settings
from reviewer.deletion_guard import deletion_guardrail
from reviewer.forgejo_client import ForgejoClient
from reviewer.verdicts import store_and_check, post_aggregated_and_gate

log = structlog.get_logger()


def _fetch_deletion_guard(forgejo: ForgejoClient, owner: str, repo: str,
                          pr_number: int) -> dict:
    """HS-4: compute the deletion guardrail from the PR's changed files + its story intent
    (PR title/body, set by the coder from the story). Best-effort — any Forgejo error
    yields a no-concern result so our own failure never blocks or crashes the review."""
    try:
        files = forgejo.get_pr_files(owner, repo, pr_number)
        try:
            pr = forgejo.get_pr(owner, repo, pr_number)
            intent = f"{pr.get('title', '')}\n{pr.get('body', '')}"
        except Exception:
            intent = ""
        return deletion_guardrail(files, intent)
    except Exception as exc:  # noqa: BLE001
        log.warning("deletion_guard_skipped", error=str(exc)[:120])
        return {"concern": False, "block": False, "removed": [], "gutted": [], "message": ""}


def _apply_deletion_guard(verdict: dict, guard: dict) -> dict:
    """Fold the deletion guardrail into the code-review verdict. An unexpected deletion is
    a hard block (force ``fail``); an expected one is surfaced as a finding but not blocked.
    Returns a new verdict dict (never mutates the input)."""
    if not guard.get("concern"):
        return verdict
    findings = list(verdict.get("findings", []))
    files = (guard.get("removed", []) + guard.get("gutted", []))
    severity = "critical" if guard.get("block") else "low"
    findings.insert(0, {"severity": severity, "file": files[0] if files else "?",
                        "line": 0, "message": guard.get("message", "")})
    new = {**verdict, "findings": findings}
    if guard.get("block"):
        new["status"] = "fail"
        new["summary"] = ("Blocked: unexpected file deletion. " +
                          (verdict.get("summary", "") or "")).strip()
    return new


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
            sug = (f.get("suggestion") or "").strip()
            if sug:
                # Surface the reviewer's proposed fix so the coding agent can apply it.
                if "\n" in sug:
                    lines.append(f"  Suggested fix:\n```\n{sug}\n```")
                else:
                    lines.append(f"  Suggested fix: `{sug}`")
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
    stack: str = "",
    story_id: str = "",
) -> dict:
    owner, repo = repo_full_name.split("/", 1)
    model = model_override or settings.model_reviewer
    log.info("code_review_start", repo=repo_full_name, pr=pr_number, sha=head_sha[:8], model=model)

    with tempfile.TemporaryDirectory() as tmpdir:
        git_ops.clone(settings.forgejo_clone_base, owner, repo, settings.effective_forgejo_token, tmpdir, branch=head_ref)
        diff = git_ops.get_diff(tmpdir, base_ref, head_sha)

    # HS-4: deterministically detect unexpected file deletions before the LLM runs, so we
    # can both surface them in the reviewer's prompt and block on them regardless of what
    # the LLM notices.
    with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as forgejo:
        guard = _fetch_deletion_guard(forgejo, owner, repo, pr_number)
    if guard.get("concern"):
        task_prompt = (task_prompt or "") + (
            "\n\n⚠️ Deletion notice — this PR removes existing files. Confirm every removal "
            "is intended and safe:\n" + guard.get("message", ""))
        log.info("deletion_guard", repo=repo_full_name, pr=pr_number,
                 block=guard.get("block"), removed=len(guard.get("removed", [])),
                 gutted=len(guard.get("gutted", [])))

    if not diff.strip():
        verdict = {"status": "pass", "summary": "No diff detected.", "findings": []}
    else:
        try:
            verdict = llm.review_diff(
                diff,
                model=model,
                api_key=settings.effective_api_key,
                system_prompt=system_prompt,
                task_prompt=task_prompt,
                stack=stack,
                story=story_id,
            )
        except llm.InsufficientCreditsError as exc:
            # Out of provider credit/quota — an unrecoverable, operator-actionable state,
            # not a code problem. Return a distinct error (no verdict stored, no review
            # comment) so the worker surfaces it once and parks the PR instead of the
            # watchdog looping on a call that can never succeed.
            log.error("code_review_insufficient_credits", repo=repo_full_name, pr=pr_number,
                      model=model, error=str(exc)[:200])
            return {"status": "error", "role": "code_review", "reason": "insufficient_credits",
                    "detail": str(exc)[:300], "operator_action": True}

    # HS-4: an unexpected deletion forces a blocking verdict even if the LLM passed it.
    verdict = _apply_deletion_guard(verdict, guard)
    verdict["role"] = "code_review"

    with ForgejoClient(settings.forgejo_base_url, settings.effective_forgejo_token) as forgejo:
        forgejo.post_pr_comment(owner, repo, pr_number, _format_review_comment(verdict))

    r = redis.from_url(settings.redis_url, decode_responses=False)
    all_in = store_and_check(r, repo_full_name, pr_number, "code_review", verdict, settings.verdict_ttl)
    if all_in:
        post_aggregated_and_gate(r, repo_full_name, pr_number, all_in)

    log.info("code_review_done", repo=repo_full_name, pr=pr_number, status=verdict.get("status"))
    return verdict
