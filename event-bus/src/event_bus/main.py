"""
Event bus — webhook receiver + work item store.

POST /webhook/forgejo — receives Forgejo webhooks
GET  /health          — liveness probe

Work items (SQLite-backed):
POST /api/ideas                    — submit idea, LLM expands, saves to SQLite
GET  /api/items                    — list all items grouped by state
GET  /api/items/{id}               — get single item with full description
POST /api/items/{id}/approve       — approve pending idea → triggers Planner Agent
POST /api/items/{id}/reject        — reject pending idea
"""

from __future__ import annotations
import asyncio
import structlog
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import redis
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from rq import Queue

import json as _json
import os as _os
from dataclasses import asdict

from collections.abc import Callable

from event_bus.config import settings
from event_bus.auth import check_basic_auth, is_exempt as auth_is_exempt
from event_bus.catalog import get_catalog, reload_catalog
from event_bus.cost_guard import over_budget
from event_bus.free_guard import free_quota_status, free_quota_exceeded
from event_bus.ci_workflow import CI_WORKFLOW_PATH
from event_bus.config_store import get_config, patch_config
from event_bus.prompt_store import get_prompt, set_prompt, delete_prompt, list_prompts
from event_bus.dispatch import dispatch_forgejo_event
from event_bus.work_store import (
    create_item, get_item, grouped_items, list_items, update_state, set_pr_url, set_repo,
    unlock_next_story, find_item_by_pr_url, get_repo_for_story, set_stack_sdlc,
    get_stack_sdlc_for_story, set_style_guides, get_style_guides_for_story,
    set_archived, delete_item_tree, list_projects, set_planning_inputs,
    STATE_COLORS, get_db
)
from event_bus.telemetry import get_telemetry_summary, render_prometheus
from event_bus.events.forgejo import ForgejoPREvent, ForgejoReviewEvent
from event_bus.signatures import verify_forgejo

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    )
)
log = structlog.get_logger()

_redis_conn: redis.Redis | None = None
_queue: Queue | None = None

_CODER_SLOT_KEY = "coder:in_flight"


def _coder_slot_acquire() -> bool:
    """Atomically claim one coding slot. Returns False if the cap is reached."""
    if not _redis_conn:
        return True
    n = _redis_conn.incr(_CODER_SLOT_KEY)
    if n > settings.max_coding_agents:
        _redis_conn.decr(_CODER_SLOT_KEY)
        return False
    return True


def _reconcile_coder_slots() -> None:
    """Reset the in-flight coder counter to the truthful startup value: 0.

    The counter tracks in-process coder asyncio tasks. A process crash or
    ``--force-recreate`` kills every such task before it can decrement, so a slot
    acquired beforehand stays consumed for the key's 24h TTL — permanently wasting
    capacity until manually reset (see the coder-slot-leak-on-recreate note). A
    fresh process has zero tracked coders, so reconcile to 0 on startup.
    """
    if not _redis_conn:
        return
    try:
        prev = int(_redis_conn.get(_CODER_SLOT_KEY) or 0)
    except (TypeError, ValueError):
        prev = 0
    _redis_conn.set(_CODER_SLOT_KEY, 0)
    if prev:
        log.warning("coder_slots_reconciled", leaked=prev)


def _reap_orphan_coder_sandboxes() -> int:
    """Remove leftover coder containers from before a restart. A fresh web process has
    spawned none, so any container labeled dev-agents.sandbox is a zombie whose thread
    died — kill it so a reclaimed story's new coder can't race it. (Tester/reviewer
    sandboxes are unlabeled + auto-remove, so they're untouched.)"""
    if settings.sandbox_mode != "docker":
        return 0
    try:
        import docker as _docker_sdk
        client = _docker_sdk.from_env()
        orphans = client.containers.list(all=True, filters={"label": "dev-agents.sandbox=true"})
        for c in orphans:
            try:
                c.remove(force=True)
            except Exception:
                pass
        if orphans:
            log.warning("orphan_coder_sandboxes_reaped", count=len(orphans))
        return len(orphans)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup, never block startup
        log.warning("reap_orphan_sandboxes_failed", error=str(exc)[:120])
        return 0


def _reconcile_orphaned_stories() -> None:
    """A restart kills in-flight coder asyncio tasks, stranding their stories in
    'in-progress' forever. On startup, return them to 'ready' and re-dispatch to fill
    the free slots — the automatic sibling of the operator Reset button."""
    orphaned = list_items(state="in-progress", item_type="story")
    if not orphaned:
        return
    for s in orphaned:
        update_state(s["id"], "ready")
    log.warning("orphaned_stories_reclaimed", count=len(orphaned))
    for _ in range(settings.max_coding_agents):
        _dispatch_next_ready()


def _reconcile_merged_stories() -> None:
    """A restart kills in-flight _await_post_merge_ci tasks, stranding stories in the
    transient 'merged' state forever (they never reach done — observed one stuck 6h). On
    startup, re-drive the post-merge CI check for each so it resolves to done (or bounces
    on a genuine failure). Runs in the async lifespan, so create_task is safe here."""
    merged = list_items(state="merged", item_type="story")
    if not merged:
        return
    for s in merged:
        repo = get_repo_for_story(s["id"]) or ""
        asyncio.create_task(_await_post_merge_ci(s["id"], repo))
    log.warning("merged_stories_reconciled", count=len(merged))


# ── Periodic watchdog ─────────────────────────────────────────────────────────
# The automatic sibling of the operator Reset/Retry buttons: a background sweep that
# re-runs work stuck too long (a coder whose task silently died, verdicts that never
# posted) instead of waiting for a restart. Bounded per story so a genuinely broken
# item is flagged for the operator rather than looped forever.
_WATCH_INTERVAL_SECS = 150
_STUCK_IN_PROGRESS_SECS = 900    # coders finish in a few min; 15 min ⇒ dead task
_STUCK_IN_REVIEW_SECS = 600      # verdicts are fast; 10 min ⇒ lost fan-out
_WATCH_ATTEMPT_CAP = 3           # after this many auto-recoveries, hand it to the operator
_CODER_ERROR_CAP = 5             # consecutive coder failures before flagging for a human


def _story_age_secs(item: dict) -> float:
    from datetime import datetime, timezone
    ts = item.get("updated_at") or item.get("created_at")
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 0.0


def _duration_secs(start: str | None, end: str | None) -> float | None:
    """Seconds between two ISO timestamps, or None if either is missing/unparseable —
    used to show how long a completed story took to build (started_at → done)."""
    from datetime import datetime, timezone
    if not start or not end:
        return None
    try:
        def _p(s):
            dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return max(0.0, (_p(end) - _p(start)).total_seconds())
    except Exception:
        return None


def is_story_stuck(item: dict) -> bool:
    """True when a story has sat in an active state past its budget — the same signal
    the watchdog acts on and the UI badges as 'stuck'."""
    state, age = item.get("state"), _story_age_secs(item)
    if state == "in-progress":
        return age > _STUCK_IN_PROGRESS_SECS
    if state in ("in-review", "changes-requested"):
        return age > _STUCK_IN_REVIEW_SECS and bool(item.get("pr_url"))
    return False


# Human-friendly labels for the agent-jobs functions, so a card can say "reviewing…"
# rather than "run_code_reviewer". Keyed by the bare function name.
_JOB_LABELS = {
    "run_code_reviewer": "reviewing",
    "run_tester": "running tests",
    "run_security_scanner": "security scan",
    "handle_pr_event": "dispatching",
    "do_merge_pr": "merging",
}


def _running_jobs_by_pr() -> dict[tuple[str, int], dict]:
    """Map (repo_full_name, pr_number) → {'label', 'state'} for jobs currently executing
    or queued on the agent-jobs queue.

    Verdict jobs (reviewer/tester/security) run while the story sits in in-review /
    changes-requested — its status never changes, so the board otherwise shows only a
    static badge and looks frozen while work is actually in flight. Reading RQ's live
    registry (rather than a heartbeat key that can go stale) reflects reality directly."""
    if _queue is None or _redis_conn is None:
        return {}
    try:
        from rq.registry import StartedJobRegistry
        from rq.job import Job
        started = StartedJobRegistry(_queue.name, connection=_redis_conn)
        running_ids = list(started.get_job_ids())
        queued_ids = list(_queue.job_ids)
    except Exception:
        return {}

    def _label(func_name: str) -> str:
        base = (func_name or "").rsplit(".", 1)[-1]
        return _JOB_LABELS.get(base, base or "working")

    out: dict[tuple[str, int], dict] = {}
    # Running first so it wins over a queued job for the same PR.
    for jid, jstate in ([(j, "running") for j in running_ids]
                        + [(j, "queued") for j in queued_ids]):
        try:
            job = Job.fetch(jid, connection=_redis_conn)
        except Exception:
            continue
        kw = job.kwargs or {}
        repo, pr = kw.get("repo_full_name"), kw.get("pr_number")
        if not repo or pr is None:
            continue
        key = (repo, int(pr))
        if out.get(key, {}).get("state") == "running":
            continue  # keep the running label already recorded
        out[key] = {"label": _label(job.func_name), "state": jstate}
    return out


def _requeue_pr_verdicts(item: dict) -> bool:
    """Recover a story stuck in in-review / changes-requested. If its PR already merged
    (a lost post-merge notification), advance it to done; otherwise re-run its verdicts.
    Runs in the watchdog worker thread, so it uses only thread-safe ops (redis + RQ +
    the DB — no asyncio)."""
    parts = _pr_url_parts(item.get("pr_url") or "")
    if not parts:
        return False
    repo_full_name, pr_number = parts
    owner, repo_name = repo_full_name.split("/", 1)
    try:
        from coding_agent.forgejo_client import ForgejoClient as CFJ
        from coding_agent.config import settings as cs
        with CFJ(cs.forgejo_base_url, cs.forgejo_api_token) as fg:
            pr = fg.get(f"/repos/{owner}/{repo_name}/pulls/{pr_number}")
    except Exception as exc:
        log.warning("watchdog_pr_fetch_failed", id=item["id"], error=str(exc)[:120])
        return False
    # Self-heal a lost post-merge advancement: the PR merged but the story never left
    # in-review. Advance it to done rather than pointlessly re-reviewing a merged PR
    # (a merged PR already passed CI, since the gate waits for CI before merging).
    if pr.get("merged"):
        log.info("watchdog_pr_already_merged", id=item["id"], pr=pr_number)
        _clear_watchdog_state(item["id"])
        update_state(item["id"], "done")
        unlock_next_story(item["id"])
        return True
    # Otherwise re-run review + tests + CI on the PR head (RQ enqueue is thread-safe).
    _clear_recovery_keys(item)
    head = pr.get("head") or {}
    from event_bus.jobs.handlers import handle_pr_event
    _queue_or_503().enqueue(
        handle_pr_event, repo_full_name=repo_full_name, pr_number=pr_number,
        head_sha=head.get("sha", ""), action="synchronized",
        head_ref=head.get("ref", ""), base_ref=(pr.get("base") or {}).get("ref", "main"))
    update_state(item["id"], "in-review")
    return True


def _sweep_stuck_stories() -> None:
    """One watchdog pass: recover stories stuck past their budget, capped per story."""
    if _redis_conn is None:
        return
    from event_bus import ratelimit
    from event_bus.config_store import get_config
    _cfg = get_config(_redis_conn)
    _coder_m = _cfg.models.coder
    _verdict_ms = [_cfg.models.reviewer, _cfg.models.tester, _cfg.models.security]
    stuck = [s for s in list_items(item_type="story") if is_story_stuck(s)]
    for s in stuck:
        # A story whose needed model is rate-limit-paused is *waiting*, not stuck — don't
        # count it against the watchdog cap or flag it for a human; it resumes on recovery.
        if s["state"] == "in-progress":
            if _coder_m and ratelimit.is_tripped(_redis_conn, _coder_m):
                continue
        elif any(m and ratelimit.is_tripped(_redis_conn, m) for m in _verdict_ms):
            continue
        key = f"watch_attempts:{s['id']}"
        try:
            n = _redis_conn.incr(key)
            _redis_conn.expire(key, 86_400)
        except Exception:
            n = 1
        if n > _WATCH_ATTEMPT_CAP:
            try:
                _redis_conn.setex(f"stuck:{s['id']}", 86_400, b"1")   # surfaced in the API
            except Exception:
                pass
            log.error("watchdog_gave_up", id=s["id"], state=s["state"],
                      attempts=n, age_secs=int(_story_age_secs(s)))
            continue
        log.warning("watchdog_recovering", id=s["id"], state=s["state"],
                    attempt=n, age_secs=int(_story_age_secs(s)))
        # Recover one story; a failure here must not abort the rest of the sweep.
        try:
            if s["state"] == "in-progress":
                _clear_recovery_keys(s)
                update_state(s["id"], "ready")  # coder slot-release chain will dispatch it
            else:  # in-review / changes-requested with a PR → re-run verdicts / advance
                _requeue_pr_verdicts(s)
        except Exception as exc:
            log.warning("watchdog_recover_failed", id=s["id"], error=str(exc)[:160])


async def _watchdog_loop() -> None:
    """Sweep for stuck stories every _WATCH_INTERVAL_SECS until the process exits."""
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_SECS)
        try:
            await asyncio.to_thread(_sweep_stuck_stories)
        except Exception as exc:  # noqa: BLE001 — a bad sweep must never kill the loop
            log.warning("watchdog_sweep_failed", error=str(exc)[:160])


def _dispatch_next_ready() -> None:
    """Claim + dispatch the next queued ready story (does NOT touch the slot counter)."""
    ready = [s for s in list_items(state="ready", item_type="story")]
    if not ready:
        return
    candidate = min(ready, key=lambda s: (s.get("sequence") or 999, s["created_at"]))
    update_state(candidate["id"], "in-progress")
    try:
        sp = get_prompt(_redis_or_503(), "coder.story")
    except Exception:
        sp = ""
    asyncio.create_task(_run_coding_agent(
        candidate["id"], candidate["title"], candidate.get("description") or "", sp,
    ))
    log.info("coder_dequeued", story_id=candidate["id"])


def _coder_slot_release_and_dispatch() -> None:
    """Release one coding slot, then trigger the next queued ready story if a slot is free."""
    if _redis_conn:
        _redis_conn.decr(_CODER_SLOT_KEY)
        _redis_conn.expire(_CODER_SLOT_KEY, 86_400)
    _dispatch_next_ready()


def _pr_url_parts(pr_url: str) -> tuple[str, int] | None:
    """(repo_full_name, pr_number) from a Forgejo PR URL (…/owner/repo/pulls/N)."""
    from urllib.parse import urlparse
    parts = urlparse(pr_url or "").path.strip("/").split("/")
    if len(parts) >= 4 and parts[-2] == "pulls" and parts[-1].isdigit():
        return f"{parts[-4]}/{parts[-3]}", int(parts[-1])
    return None


def _clear_recovery_keys(item: dict) -> None:
    """Clear the Redis caps/counters that gate a story so a retry starts clean:
    the post-merge fix cap, the stale agent log, and (if it has a PR) the recode cap."""
    if not _redis_conn:
        return
    _redis_conn.delete(f"post_merge_fix:{item['id']}")
    _redis_conn.delete(f"agent_log:{item['id']}")
    parts = _pr_url_parts(item.get("pr_url") or "")
    if parts:
        _redis_conn.delete(f"recode_retries:{parts[0]}:{parts[1]}")


def _clear_watchdog_state(item_id: str) -> None:
    """Reset the watchdog's per-story attempt counter + stuck flag — called when the
    operator takes over (Retry/Reset), so auto-recovery starts fresh afterward."""
    if _redis_conn:
        _redis_conn.delete(f"watch_attempts:{item_id}", f"stuck:{item_id}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis_conn, _queue
    # Warn loudly if secrets are absent — fail-closed means webhooks will be
    # rejected with 403 until secrets are configured.
    if not settings.forgejo_webhook_secret:
        log.warning("forgejo_webhook_secret_not_set — all Forgejo webhooks will be rejected")
    if not settings.board_auth_password:
        log.warning("board_auth_disabled — board UI/API is OPEN; set BOARD_AUTH_PASSWORD "
                    "to require login (anyone reaching the URL can drive LLM cost)")
    _redis_conn = redis.from_url(settings.redis_url, decode_responses=False)
    _queue = Queue("agent-jobs", connection=_redis_conn)
    _reconcile_coder_slots()  # clear any slot leaked by a prior crash/recreate
    get_db()  # initialise SQLite schema
    _reap_orphan_coder_sandboxes()   # kill zombie coder containers from before restart
    _reconcile_orphaned_stories()    # resume stories stranded in-progress by the restart
    _reconcile_merged_stories()      # re-drive stories stranded in the transient 'merged' state
    watchdog = asyncio.create_task(_watchdog_loop())  # continuous stuck-job recovery
    try:  # warm the model-meta cache so agents can route structured outputs immediately
        from event_bus.models_catalog import refresh_free_models
        await asyncio.to_thread(refresh_free_models, _redis_conn, settings.openrouter_api_key)
    except Exception as exc:
        log.warning("model_meta_warm_failed", error=str(exc)[:120])
    log.info("event_bus_started", redis=settings.redis_url)
    yield
    watchdog.cancel()
    if _redis_conn:
        _redis_conn.close()


app = FastAPI(title="dev-agents event bus", lifespan=lifespan)


@app.middleware("http")
async def board_basic_auth(request: Request, call_next):
    """Require HTTP Basic Auth for the board UI/API when a password is configured.
    Webhook (HMAC), /internal (service-to-service), and /health are exempt."""
    if settings.board_auth_password and not auth_is_exempt(request.url.path):
        if not check_basic_auth(
            request.headers.get("authorization", ""),
            settings.board_auth_user,
            settings.board_auth_password,
        ):
            return Response(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content="Authentication required",
                headers={"WWW-Authenticate": 'Basic realm="Agentic Dev Team board"'},
            )
    return await call_next(request)


# Serve the control-panel UI from /ui/
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/ui", StaticFiles(directory=str(_static_dir), html=True), name="ui")


@app.get("/", include_in_schema=False)
async def root():
    """Redirect bare root to the control-panel UI."""
    return RedirectResponse("/ui/")


def _queue_or_503() -> Queue:
    if _queue is None:
        raise HTTPException(status_code=503, detail="queue not ready")
    return _queue


def _redis_or_503() -> redis.Redis:
    if _redis_conn is None:
        raise HTTPException(status_code=503, detail="redis not ready")
    return _redis_conn


_LOG_TTL = 86_400  # keep agent logs for 24 h


def _make_log_cb(item_id: str) -> Callable[[str], None]:
    """Return a callable that pushes each agent output line to a Redis list."""
    key = f"agent_log:{item_id}"
    if _redis_conn:
        _redis_conn.delete(key)

    def _push(line: str) -> None:
        r = _redis_conn
        if r:
            r.rpush(key, line.encode("utf-8", errors="replace"))

    return _push


def _expire_log(item_id: str) -> None:
    if _redis_conn:
        _redis_conn.expire(f"agent_log:{item_id}", _LOG_TTL)


# ── Forgejo webhook ────────────────────────────────────────────────────────────

@app.post("/webhook/forgejo", status_code=status.HTTP_202_ACCEPTED)
async def forgejo_webhook(
    request: Request,
    x_gitea_event: str | None = Header(default=None),
    x_gitea_signature: str | None = Header(default=None),
):
    body = await request.body()
    log.info("forgejo_webhook_received", gitea_event=x_gitea_event, size=len(body))

    # Always validate — verify_forgejo returns False when secret is empty (fail-closed)
    if not verify_forgejo(body, x_gitea_signature or "", settings.forgejo_webhook_secret):
        log.warning("forgejo_signature_invalid")
        raise HTTPException(status_code=403, detail="invalid signature")

    _REVIEW_EVENTS = {
        "pull_request_review",
        "pull_request_review_rejected",
        "pull_request_review_approved",
        "pull_request_review_comment",
    }

    try:
        payload = _json.loads(body)
    except _json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc

    # Handle "changes requested" review events
    if x_gitea_event in _REVIEW_EVENTS:
        try:
            review_event = ForgejoReviewEvent.model_validate(payload)
        except ValidationError as exc:
            log.warning("forgejo_review_parse_error", error=str(exc))
            return {"result": "skipped", "reason": "review payload parse failed"}

        log.info("forgejo_review_event",
                 gitea_event=x_gitea_event,
                 review_type=review_event.review.type,
                 review_body=review_event.review.body[:100],
                 pr=review_event.pr_number)

        if not review_event.is_changes_requested():
            return {"result": "skipped", "reason": f"review type not changes-requested: {review_event.review.type!r}"}

        asyncio.create_task(_run_recode_agent(review_event))
        log.info("forgejo_review_webhook", pr=review_event.pr_number, repo=review_event.repo_full_name)
        return {"result": "accepted", "reason": "changes_requested"}

    if x_gitea_event != "pull_request":
        return {"result": "skipped", "reason": f"unhandled event type: {x_gitea_event}"}

    try:
        event = ForgejoPREvent.model_validate(payload)
    except ValidationError as exc:
        log.warning("forgejo_parse_error", error=str(exc))
        raise HTTPException(status_code=400, detail="payload validation failed") from exc

    # Auto-advance story when PR is merged in Forgejo. 'merged' is transient — the
    # post-merge CI gate (in _on_story_merged) drives done/fix + unlocks the next story.
    if event.action == "closed" and event.pull_request.merged:
        pr_url = event.pull_request.html_url
        item = find_item_by_pr_url(pr_url)
        if item:
            if item["state"] != "merged":  # ignore duplicate merged webhooks
                _on_story_merged(item["id"], event.repo_full_name)
            log.info("pr_merged_auto_advance", item_id=item["id"], pr=event.pr_number)
            return {"result": "merged", "item_id": item["id"]}
        log.info("pr_merged_no_story", pr_url=pr_url)
        return {"result": "skipped", "reason": "no matching story for PR"}

    # A new commit on the PR from a HUMAN resets the recode retry counter (give the
    # human's fix fresh auto-fix attempts). The coder-bot's OWN recode commits must
    # NOT reset it — otherwise every recode resets the cap and recodes loop forever.
    if event.action in ("synchronize", "synchronized"):
        pusher = (event.sender or {}).get("login", "")
        if pusher and pusher != settings.forgejo_coder_user:
            retry_key = f"recode_retries:{event.repo_full_name}:{event.pr_number}"
            _redis_or_503().delete(retry_key)

    outcome = dispatch_forgejo_event(event, _queue_or_503())
    log.info("forgejo_webhook", result=outcome.result, reason=outcome.reason, job=outcome.job_id)
    return {"result": outcome.result, "job_id": outcome.job_id}


# ── Ideas API ─────────────────────────────────────────────────────────────────

def _normalize_decisions(raw) -> list[dict]:
    """Clean the idea agent's design decisions into a stable, bounded shape. `chosen`
    starts unset — the operator fills it (or the recommendation is auto-accepted)."""
    out = []
    for i, d in enumerate((raw or [])[:7]):
        if not isinstance(d, dict) or not d.get("question"):
            continue
        alts = [str(a) for a in (d.get("alternatives") or []) if a][:4]
        out.append({
            "id": f"d{i}",
            "question": str(d["question"])[:200],
            "recommended": str(d.get("recommended") or "")[:200],
            "rationale": str(d.get("rationale") or "")[:300],
            "alternatives": alts,
            "chosen": None,
        })
    return out


def _format_decisions(decisions_json: str | None) -> str:
    """Render the operator's answered design decisions as a constraints block for the
    planner. Empty when there are none."""
    try:
        decisions = _json.loads(decisions_json or "[]")
    except Exception:
        return ""
    lines = [f"- {d['question']} → {d['chosen']}" for d in decisions if d.get("chosen")]
    return ("The operator has LOCKED these design decisions — the plan MUST honor them:\n"
            + "\n".join(lines)) if lines else ""


@app.post("/api/ideas", status_code=status.HTTP_202_ACCEPTED)
async def submit_idea(request: Request):
    """
    Submit a new feature idea. The Idea Agent expands the prompt with an LLM
    and saves it to SQLite with state 'pending-approval'.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="'prompt' field required")

    cfg = get_config(_redis_or_503())
    if over_budget(_redis_or_503(), cfg.limits.max_cost_usd_daily):
        raise HTTPException(
            status_code=429,
            detail=f"daily LLM cost cap reached (${cfg.limits.max_cost_usd_daily}); "
                   "new ideas are paused. Raise max_cost_usd_daily or wait for the next day.",
        )
    model_override = (body.get("model_override") or "").strip() or cfg.models.idea

    cat = get_catalog()
    stack_options = [{"id": s.id, "display_name": s.display_name} for s in cat.list_stacks()]
    sdlc_options = [{"id": s.id, "display_name": s.display_name} for s in cat.list_sdlc()]
    style_options = [{"id": g.id, "display_name": g.display_name} for g in cat.list_style_guides()]

    try:
        from idea_agent.main import expand_idea
        proposal = expand_idea(prompt, model_override=model_override, redis_conn=_redis_conn,
                               stack_options=stack_options, sdlc_options=sdlc_options,
                               style_guide_options=style_options)
    except ImportError:
        raise HTTPException(status_code=503, detail="idea_agent package not installed")
    except Exception as exc:
        log.error("idea_expansion_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

    # Constrain the proposal to the catalog; unknown/empty resolves to generic/standard.
    stack = cat.get_stack(proposal.get("proposed_stack")).id
    sdlc = cat.get_sdlc(proposal.get("proposed_sdlc")).id
    # Keep proposed guides that exist AND apply to the chosen stack.
    applicable = {g.id for g in cat.style_guides_for_stack(stack)}
    guides = [g for g in (proposal.get("proposed_style_guides") or []) if g in applicable]
    decisions = _normalize_decisions(proposal.get("design_decisions"))

    item = create_item(
        item_type="idea",
        title=proposal["title"],
        prompt=prompt,
        description=proposal.get("description", ""),
        state="pending-approval",
        model_used=model_override or "",
        stack=stack,
        sdlc=sdlc,
        stack_rationale=(proposal.get("stack_rationale") or "")[:500],
        style_guides=guides,
        design_decisions=_json.dumps(decisions) if decisions else "",
    )
    log.info("idea_submitted", id=item["id"], title=item["title"], stack=stack, sdlc=sdlc,
             guides=guides)
    return item


@app.post("/api/ideas/import", status_code=status.HTTP_202_ACCEPTED)
async def import_plan(request: Request):
    """Import an externally-authored plan (pasted PRD/markdown). Skips idea expansion
    and auto-decomposition: the plan is normalized into our epic/story model and held
    at the plan-approval gate for review."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    plan_text = (body.get("plan_text") or "").strip()
    if len(plan_text) < 40:
        raise HTTPException(status_code=400, detail="'plan_text' is required (paste the plan)")

    cfg = get_config(_redis_or_503())
    if over_budget(_redis_or_503(), cfg.limits.max_cost_usd_daily):
        raise HTTPException(status_code=429, detail="daily LLM cost cap reached; imports paused")

    cat = get_catalog()
    stack = body.get("stack") or GENERIC_STACK_ID
    sdlc = body.get("sdlc") or "standard"
    if not cat.has_stack(stack):
        raise HTTPException(status_code=422, detail=f"unknown stack: {stack!r}")
    if not cat.has_sdlc(sdlc):
        raise HTTPException(status_code=422, detail=f"unknown sdlc: {sdlc!r}")
    guides = [g for g in (body.get("style_guides") or []) if cat.has_style_guide(g)]
    planner_model = (body.get("planner_model") or "").strip()
    title = (body.get("title") or "Imported plan").strip()[:80]

    item = create_item(
        item_type="idea", title=title, prompt=plan_text,
        description="Imported plan — normalized into epics + stories, held for review.",
        state="approved", stack=stack, sdlc=sdlc, style_guides=guides,
        planner_model=planner_model,
    )
    log.info("plan_imported", id=item["id"], title=title, chars=len(plan_text),
             stack=stack, sdlc=sdlc, model=planner_model or "default")
    asyncio.create_task(_run_import(item["id"]))
    return item


# ── Work items API ────────────────────────────────────────────────────────────

def _fetch_verdicts(pr_url: str) -> dict:
    """Look up the three reviewer/tester/security verdicts for a PR from Redis."""
    if not _redis_conn or not pr_url:
        return {}
    try:
        from urllib.parse import urlparse
        parts = urlparse(pr_url).path.strip("/").split("/")
        # path: owner/repo/pulls/number
        if len(parts) < 4 or parts[-2] != "pulls":
            return {}
        owner, repo, pr_number = parts[0], parts[1], parts[3]
        base = f"pr_verdict:{owner}:{repo}:{pr_number}"
        role_map = [
            ("reviewer", f"{base}:code_review"),
            ("tester",   f"{base}:test_run"),
            ("security", f"{base}:security"),
        ]
        verdicts = {}
        for role, key in role_map:
            raw = _redis_conn.get(key)
            if raw:
                verdicts[role] = _json.loads(raw).get("status")  # "pass"|"warn"|"fail"
        return verdicts
    except Exception:
        return {}


def _rate_limited_from_ratelimit() -> list:
    from event_bus import ratelimit
    return ratelimit.tripped_models(_redis_conn)


@app.get("/api/items")
async def list_work_items(response: Response):
    """Return all work items grouped by state with state metadata."""
    response.headers["Cache-Control"] = "no-store"
    groups = grouped_items()
    _active = ("in-progress", "in-review", "changes-requested")
    running_by_pr = _running_jobs_by_pr()  # (repo, pr) → live/queued agent job
    # Enrich items with live verdict data + runtime signals (elapsed / stale / stuck).
    for items in groups.values():
        for item in items:
            if item.get("pr_url"):
                item["verdicts"] = _fetch_verdicts(item["pr_url"])
            if item.get("type") == "story" and item.get("state") in _active:
                item["age_secs"] = int(_story_age_secs(item))
                item["stale"] = is_story_stuck(item)        # past its budget (amber)
                item["stuck"] = bool(_redis_conn and _redis_conn.get(f"stuck:{item['id']}"))
                # A live verdict job for this PR means work is in flight even though the
                # story status hasn't moved — surface it so the card shows "reviewing…".
                parts = _pr_url_parts(item.get("pr_url") or "")
                if parts and parts in running_by_pr:
                    item["running_job"] = running_by_pr[parts]
            elif item.get("type") == "story" and item.get("state") == "done":
                dur = _duration_secs(item.get("started_at"), item.get("updated_at"))
                if dur is not None:
                    item["duration_secs"] = int(dur)
                    # Historical stories were backfilled with an estimated start time
                    # (they predate started_at stamping) — flag so the pill reads "~".
                    item["duration_approx"] = bool(
                        _redis_conn and _redis_conn.get(f"approx_duration:{item['id']}")
                    )
    return {
        "groups": [
            {
                "state": state,
                "color": STATE_COLORS.get(state, "#888"),
                "items": items,
            }
            for state, items in groups.items()
        ],
        "total": sum(len(v) for v in groups.values()),
        # Models currently paused by the rate-limit circuit breaker → drives the "paused"
        # banner so the operator sees the fleet is waiting (auto-retrying), not dead.
        "rate_limited": _rate_limited_from_ratelimit(),
    }


@app.get("/api/items/{item_id}")
async def get_work_item(item_id: str):
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    return item


@app.post("/api/items/{item_id}/approve", status_code=status.HTTP_200_OK)
async def approve_item(item_id: str, request: Request):
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["state"] != "pending-approval":
        raise HTTPException(status_code=409, detail=f"item is '{item['state']}', not pending-approval")

    # Optional stack/SDLC overrides — validated against the catalog (reject invalid).
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    cat = get_catalog()
    new_stack, new_sdlc = body.get("stack"), body.get("sdlc")
    new_guides = body.get("style_guides")
    if new_stack is not None and not cat.has_stack(new_stack):
        raise HTTPException(status_code=422, detail=f"unknown stack: {new_stack!r}")
    if new_sdlc is not None and not cat.has_sdlc(new_sdlc):
        raise HTTPException(status_code=422, detail=f"unknown sdlc: {new_sdlc!r}")
    if new_guides is not None:
        if not isinstance(new_guides, list):
            raise HTTPException(status_code=422, detail="style_guides must be a list")
        for g in new_guides:
            if not cat.has_style_guide(g):
                raise HTTPException(status_code=422, detail=f"unknown style guide: {g!r}")
    if new_stack is not None or new_sdlc is not None:
        set_stack_sdlc(
            item_id,
            new_stack if new_stack is not None else item.get("stack"),
            new_sdlc if new_sdlc is not None else item.get("sdlc"),
        )
    if new_guides is not None:
        set_style_guides(item_id, new_guides)

    # Design decisions: merge the operator's answers into the stored decisions
    # (unanswered ones auto-accept the recommendation). Plus an optional planner model.
    answers = body.get("design_answers") or {}
    planner_model = (body.get("planner_model") or "").strip()
    try:
        decisions = _json.loads(item.get("design_decisions") or "[]")
    except Exception:
        decisions = []
    for d in decisions:
        d["chosen"] = (answers.get(d["id"]) or d.get("recommended") or "").strip()
    set_planning_inputs(
        item_id,
        design_decisions=_json.dumps(decisions) if decisions else None,
        planner_model=planner_model or None,
    )

    updated = update_state(item_id, "approved")
    final = get_item(item_id) or item
    log.info("idea_approved", id=item_id, title=item["title"],
             stack=final.get("stack"), sdlc=final.get("sdlc"),
             guides=final.get("style_guides"))
    # Kick off planner in the background so the response returns immediately
    asyncio.create_task(_run_planner(item_id, item["title"], item["description"] or ""))
    return updated


def _slugify(text: str) -> str:
    """Convert a title to a valid Forgejo repo name (lowercase, hyphens, max 40 chars)."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40]


def _provision_project_repo(idea_id: str, title: str, stack_id: str | None = None) -> str:
    """
    Create a Forgejo repo for this idea (if it doesn't exist), commit the stack's
    CI workflow + scaffold + stack marker, protect main, grant the bots, and
    register the webhook. Returns 'owner/repo' or '' on failure.
    """
    try:
        from coding_agent.forgejo_client import ForgejoClient
        from coding_agent.config import settings as coding_settings
    except ImportError:
        log.warning("coding_agent_not_installed_skipping_repo_provision")
        return ""

    stack = get_catalog().get_stack(stack_id)

    repo_name = _slugify(title)
    if not repo_name:
        return ""
    owner = coding_settings.forgejo_base_url.rstrip("/").split("/")[-1] if False else "devadmin"

    # Determine owner from Forgejo API (the token owner)
    try:
        import httpx
        resp = httpx.get(
            f"{coding_settings.forgejo_base_url}/api/v1/user",
            headers={"Authorization": f"token {coding_settings.forgejo_api_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        owner = resp.json().get("login", "devadmin")
    except Exception as exc:
        log.warning("forgejo_user_lookup_failed", error=str(exc))

    repo_full = f"{owner}/{repo_name}"
    try:
        with ForgejoClient(coding_settings.forgejo_base_url, coding_settings.forgejo_api_token) as fj:
            created = False
            if not fj.repo_exists(owner, repo_name):
                fj.create_repo(repo_name, description=title)
                created = True
            # On fresh repos commit the stack's CI workflow + scaffold + a stack
            # marker in ONE commit, so the Actions runner gets a single clean run on
            # the complete scaffolded project (not several failing intermediate runs).
            if created:
                files = {
                    CI_WORKFLOW_PATH: stack.ci_workflow,
                    ".devagents/stack": stack.id + "\n",
                    **stack.scaffold,
                }
                try:
                    fj.create_files(owner, repo_name, files,
                                    message=f"chore: scaffold {stack.id} project + CI",
                                    branch="main")
                except Exception as exc:
                    log.warning("scaffold_commit_failed", repo=repo_full, stack=stack.id, error=str(exc))
                # Protect main: block direct pushes (agents work via PRs). No required
                # approvals/status — the reviewer auto-merges and a hard gate would
                # deadlock that; the event-bus verdicts remain the real gate.
                try:
                    fj.set_branch_protection(owner, repo_name, "main")
                except Exception as exc:
                    log.warning("branch_protection_failed", repo=repo_full, error=str(exc))
                # Grant the agent bots write access so they operate with their own
                # least-privilege tokens (coder authors PRs, reviewer comments/merges)
                # instead of the admin token.
                for bot in (settings.forgejo_coder_user, settings.forgejo_reviewer_user):
                    if bot and bot != owner:
                        try:
                            fj.add_collaborator(owner, repo_name, bot, "write")
                        except Exception as exc:
                            log.warning("bot_collaborator_failed", repo=repo_full, bot=bot, error=str(exc))
            # Register webhook (idempotent — Forgejo allows duplicates but we accept that)
            webhook_url = f"http://event-bus:8080/webhook/forgejo"
            fj.create_webhook(owner, repo_name, webhook_url, settings.forgejo_webhook_secret)
        log.info("project_repo_provisioned", repo=repo_full, idea=idea_id,
                 stack=stack.id, scaffolded=created)
    except Exception as exc:
        log.error("project_repo_provision_failed", repo=repo_full, error=str(exc))
        return ""

    return repo_full


def _stack_id_for_repo(owner: str, repo: str) -> str:
    """Resolve a repo's stack id from its committed `.devagents/stack` marker,
    falling back to generic. Used by steps that only have the repo (recode, tester)."""
    from event_bus.catalog import GENERIC_STACK_ID
    try:
        import base64
        from coding_agent.forgejo_client import ForgejoClient
        from coding_agent.config import settings as cs
        with ForgejoClient(cs.forgejo_base_url, cs.forgejo_api_token) as fj:
            data = fj.get(f"/repos/{owner}/{repo}/contents/.devagents/stack")
        content = base64.b64decode(data.get("content", "")).decode().strip()
        return content if get_catalog().has_stack(content) else GENERIC_STACK_ID
    except Exception:
        return GENERIC_STACK_ID


async def _run_planner(item_id: str, title: str, description: str) -> None:
    """Run the Planner Agent in a thread and save resulting stories to SQLite."""
    cfg = get_config(_redis_or_503())
    if over_budget(_redis_conn, cfg.limits.max_cost_usd_daily):
        log.warning("planner_skipped_cost_cap", id=item_id)
        return

    # Resolve the stack chosen at approval (falls back to generic/standard).
    item = get_item(item_id) or {}
    cat = get_catalog()
    stack = cat.get_stack(item.get("stack"))
    sdlc = cat.get_sdlc(item.get("sdlc"))
    # Operator's per-project planning model (chosen at approval) overrides the config.
    model = (item.get("planner_model") or "").strip() or cfg.models.planner
    decisions_block = _format_decisions(item.get("design_decisions"))

    # Provision a dedicated Forgejo repo for this project before decomposing.
    repo_full = await asyncio.to_thread(_provision_project_repo, item_id, title, stack.id)
    if repo_full:
        set_repo(item_id, repo_full)
        log.info("idea_repo_set", idea=item_id, repo=repo_full)

    try:
        from planner_agent.main import run_planner
        plan = await asyncio.to_thread(
            run_planner, item_id, title, description, model,
            repo_full_name=repo_full,
            sdlc_directive=sdlc.planner_directive,
            best_practices=stack.best_practices_prompt,
            stack=stack.id,
            decisions=decisions_block,
            redis_conn=_redis_conn,
        )
    except ImportError:
        log.error("planner_agent_not_installed")
        return
    except Exception as exc:
        log.error("planner_failed", id=item_id, error=str(exc))
        return

    _persist_plan(item_id, plan, repo_full, stack, sdlc, model, cfg)


def _persist_plan(item_id: str, plan: dict, repo_full: str, stack, sdlc,
                  model: str, cfg, seq_offset: int = 0) -> None:
    """Create the plan's epic-tagged stories and apply the plan-approval gate. Shared by
    the auto-planner (_run_planner) and the plan importer (_run_import). ``seq_offset``
    continues sequence numbering when appending to an existing plan (resume)."""
    item = get_item(item_id) or {}
    stories = plan.get("stories", [])
    idea_guides = [g for g in (item.get("style_guides") or "").split(",") if g]
    # Plan-approval gate: when ON, every story is parked in backlog until the operator
    # approves the plan; when OFF, the first story starts immediately (legacy behavior).
    # A resumed append (seq_offset > 0) never auto-starts — the plan is mid-review.
    plan_gate = cfg.gates.plan_approval or seq_offset > 0
    first_story: dict = {}
    for i, story in enumerate(stories):
        state = "backlog" if (plan_gate or i > 0) else "ready"
        seq = seq_offset + i + 1
        story_item = create_item(
            item_type="story",
            title=story["title"],
            description=story.get("description", ""),
            state=state,
            parent_id=item_id,
            sequence=seq,
            model_used=model,
            repo=repo_full,
            stack=stack.id,
            sdlc=sdlc.id,
            style_guides=idea_guides,
            epic=story.get("epic", ""),
        )
        log.info("story_created", id=story_item["id"], seq=seq, state=state,
                 epic=story.get("epic", ""), title=story_item["title"], repo=repo_full)
        if i == 0:
            first_story = story_item

    log.info("planner_complete", idea_id=item_id,
             module=plan.get("project_name") or plan.get("module_name"),
             epics=len(plan.get("epics", [])), story_count=len(stories),
             repo=repo_full, seq_offset=seq_offset)

    if stories and not plan_gate:
        update_state(first_story["id"], "in-progress")
        story_prompt = get_prompt(_redis_conn, "coder.story")
        asyncio.create_task(_run_coding_agent(
            first_story["id"], first_story["title"], first_story["description"] or "", story_prompt,
        ))
        log.info("coding_agent_auto_triggered", story_id=first_story["id"])
    elif stories:
        log.info("plan_awaiting_approval", idea_id=item_id,
                 epics=len(plan.get("epics", [])), stories=len(stories))


async def _run_import(item_id: str) -> None:
    """Normalize an imported (externally-authored) plan into epic-tagged stories:
    provision the repo, run the normalizer, then persist through the shared gate."""
    cfg = get_config(_redis_or_503())
    item = get_item(item_id) or {}
    plan_text = item.get("prompt") or ""
    cat = get_catalog()
    stack = cat.get_stack(item.get("stack"))
    sdlc = cat.get_sdlc(item.get("sdlc"))
    model = (item.get("planner_model") or "").strip() or cfg.models.planner

    repo_full = await asyncio.to_thread(_provision_project_repo, item_id, item.get("title", ""), stack.id)
    if repo_full:
        set_repo(item_id, repo_full)
        log.info("idea_repo_set", idea=item_id, repo=repo_full)

    try:
        from planner_agent.main import run_import
        plan = await asyncio.to_thread(
            run_import, item_id, plan_text, model,
            repo_full_name=repo_full, stack=stack.id, redis_conn=_redis_conn)
    except Exception as exc:
        log.error("import_failed", id=item_id, error=str(exc))
        return
    _store_import_epics(item_id, plan.get("epics", []))
    _persist_plan(item_id, plan, repo_full, stack, sdlc, model, cfg)


_IMPORT_EPICS_KEY = "import:epics:{}"


def _store_import_epics(item_id: str, epics: list[dict]) -> None:
    """Persist the pass-1 epic list so a later resume reuses it verbatim (epic identity
    is stable across runs — pass 1 is non-deterministic and would otherwise rename
    epics, breaking skip matching and duplicating work)."""
    if not epics:
        return
    try:
        _redis_or_503().set(_IMPORT_EPICS_KEY.format(item_id), _json.dumps(epics))
    except Exception as exc:
        log.warning("import_epics_store_failed", id=item_id, error=str(exc)[:120])


def _load_import_epics(item_id: str) -> list[dict]:
    try:
        raw = _redis_or_503().get(_IMPORT_EPICS_KEY.format(item_id))
        return _json.loads(raw) if raw else []
    except Exception:
        return []


async def _resume_import(item_id: str) -> None:
    """Fill the epics a prior import couldn't finish (e.g. cut short by a rate limit):
    re-run pass 1 to get the full epic list, skip epics that already have stories, and
    normalize only the missing ones — appending them without redoing completed work."""
    cfg = get_config(_redis_or_503())
    item = get_item(item_id) or {}
    plan_text = item.get("prompt") or ""
    cat = get_catalog()
    stack = cat.get_stack(item.get("stack"))
    sdlc = cat.get_sdlc(item.get("sdlc"))
    model = (item.get("planner_model") or "").strip() or cfg.models.planner
    repo_full = item.get("repo") or ""

    existing = [s for s in list_items(item_type="story") if s.get("parent_id") == item_id]
    covered = {s.get("epic", "") for s in existing if s.get("epic")}
    seq_offset = max((s.get("sequence") or 0 for s in existing), default=0)
    # Reuse the epic list captured on the first run so epic identity matches `covered`
    # exactly (older imports predating this have no stored list → pass 1 re-runs).
    stored_epics = _load_import_epics(item_id)
    log.info("import_resume_start", id=item_id, existing=len(existing),
             covered_epics=len(covered), seq_offset=seq_offset, reuse_epics=len(stored_epics))

    try:
        from planner_agent.main import run_import
        plan = await asyncio.to_thread(
            run_import, item_id, plan_text, model,
            repo_full_name=repo_full, stack=stack.id, redis_conn=_redis_conn,
            skip_epics=covered, epics=stored_epics or None)
    except Exception as exc:
        log.error("import_resume_failed", id=item_id, error=str(exc))
        return
    new_stories = plan.get("stories", [])
    if not new_stories:
        log.warning("import_resume_no_new_stories", id=item_id)
        return
    _persist_plan(item_id, plan, repo_full, stack, sdlc, model, cfg, seq_offset=seq_offset)


@app.post("/api/items/{item_id}/resume-import", status_code=status.HTTP_202_ACCEPTED)
async def resume_import(item_id: str):
    """Resume a partial plan import — normalize only the epics still missing stories.

    Safe to call repeatedly (e.g. after a subscription rate-limit window resets); each
    call fills whatever epics remain until the plan is complete."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["type"] != "idea":
        raise HTTPException(status_code=409, detail="only imported projects can be resumed")
    if not (item.get("prompt") or "").strip():
        raise HTTPException(status_code=409, detail="no source plan stored to resume from")
    stories = [s for s in list_items(item_type="story") if s.get("parent_id") == item_id]
    if stories and any(s["state"] not in ("backlog",) for s in stories):
        raise HTTPException(status_code=409,
                            detail="plan already started — resume only before approval")
    log.info("import_resume_requested", id=item_id, existing=len(stories))
    asyncio.create_task(_resume_import(item_id))
    return {"status": "resuming", "id": item_id, "existing_stories": len(stories)}


@app.post("/api/items/{item_id}/code", status_code=status.HTTP_202_ACCEPTED)
async def code_item(item_id: str):
    """Claim a ready story and run the Coding Agent asynchronously."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["type"] != "story":
        raise HTTPException(status_code=409, detail="only stories can be coded")
    if item["state"] != "ready":
        raise HTTPException(status_code=409, detail=f"story is '{item['state']}', not ready")
    # Claim atomically before returning
    update_state(item_id, "in-progress")
    story_prompt = get_prompt(_redis_or_503(), "coder.story")
    asyncio.create_task(_run_coding_agent(item_id, item["title"], item["description"] or "", story_prompt))
    return {"status": "coding_started", "id": item_id}


# Env passed into the ephemeral coding-agent sandbox container. FORGEJO_CODER_TOKEN
# is required so the coder authenticates as the least-privilege coder-bot, not admin.
_CODER_SANDBOX_ENV = [
    "FORGEJO_API_TOKEN", "FORGEJO_CODER_TOKEN", "FORGEJO_BASE_URL", "FORGEJO_GIT_URL",
    "OPENROUTER_API_KEY", "MODEL_CODER", "DEFAULT_REPO", "ANTHROPIC_API_KEY",
]


def _coder_context(item_id: str):
    """Resolve the (stack, sdlc) catalog entries for a story (with inheritance)."""
    stack_id, sdlc_id = get_stack_sdlc_for_story(item_id)
    cat = get_catalog()
    return cat.get_stack(stack_id), cat.get_sdlc(sdlc_id)


def _augment_coder_prompt(story_prompt: str, stack, sdlc, guides=()) -> str:
    """Prepend stack conventions + SDLC coder directive + style guides to the prompt."""
    extra = []
    if stack.best_practices_prompt.strip():
        extra.append("Stack conventions:\n" + stack.best_practices_prompt.strip())
    if sdlc.coder_directive.strip():
        extra.append("Development style:\n" + sdlc.coder_directive.strip())
    for g in guides:
        if g.prompt.strip():
            extra.append(f"Style guide — {g.display_name}:\n" + g.prompt.strip())
    if not extra:
        return story_prompt
    return (story_prompt or "").rstrip() + "\n\n" + "\n\n".join(extra)


def _read_container_json(container, path: str) -> dict | None:
    """Read + parse a JSON file out of a (stopped, not-yet-removed) container via the
    Docker archive API — no shared volume needed. Returns None if absent/unparseable."""
    import io
    import tarfile
    try:
        stream, _ = container.get_archive(path)
    except Exception:
        return None
    try:
        buf = io.BytesIO()
        for chunk in stream:
            buf.write(chunk)
        buf.seek(0)
        with tarfile.open(fileobj=buf) as tar:
            member = tar.next()
            if member is None:
                return None
            data = tar.extractfile(member).read()
        return _json.loads(data.decode("utf-8"))
    except Exception:
        return None


def _run_coding_agent_sandboxed_sync(
    item_id: str,
    title: str,
    description: str,
    story_prompt: str,
    log_cb,
    coder_image: str = "",
    test_command: str = "",
    install_command: str = "",
    model: str = "",
) -> dict:
    """Blocking: spawn an ephemeral Docker container for one coding agent run.
    Returns the result dict from sandbox_runner (same shape as run_coding_agent).
    """
    import docker as _docker_sdk

    client = _docker_sdk.from_env()

    env = {k: _os.environ[k] for k in _CODER_SANDBOX_ENV if k in _os.environ}
    if model:
        env["MODEL_CODER"] = model   # runtime-config override wins over the container env
    env.update({
        "STORY_ID": item_id,
        "STORY_TITLE": title[:500],
        "STORY_DESCRIPTION": (description or "")[:4000],
        "STORY_PROMPT": (story_prompt or "")[:4000],
        "STORY_TEST_CMD": test_command or "",
        "STORY_INSTALL_CMD": install_command or "",
    })

    volumes: dict = {}
    if settings.sandbox_opencode_bin:
        volumes[settings.sandbox_opencode_bin] = {"bind": "/usr/local/bin/opencode", "mode": "ro"}
    if settings.sandbox_opencode_config:
        volumes[settings.sandbox_opencode_config] = {"bind": "/root/.config/opencode", "mode": "ro"}

    create_kwargs = dict(
        command=["python", "-m", "coding_agent.sandbox_runner"],
        environment=env,
        volumes=volumes or None,
        network=settings.sandbox_network,
        mem_limit=settings.sandbox_memory,
        nano_cpus=int(float(settings.sandbox_cpus) * 1_000_000_000),
        labels={"dev-agents.story-id": item_id, "dev-agents.sandbox": "true"},
    )
    image = coder_image or settings.sandbox_image
    try:
        container = client.containers.create(image=image, **create_kwargs)
    except _docker_sdk.errors.ImageNotFound:
        # Per-stack image not built yet — fall back to the default coder image.
        log.warning("coder_image_unavailable_fallback", requested=image,
                    fallback=settings.sandbox_image)
        container = client.containers.create(image=settings.sandbox_image, **create_kwargs)

    try:
        container.start()
        result_json: dict | None = None

        for raw in container.logs(stream=True, follow=True):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line.startswith("CODING_RESULT:"):
                try:
                    result_json = _json.loads(line[len("CODING_RESULT:"):])
                except Exception:
                    pass
            elif log_cb and line.strip():
                log_cb(line)

        exit_info = container.wait()
        exit_code = exit_info.get("StatusCode", -1)

        # Authoritative: the result file written inside the container (read while it
        # still exists, before remove()). Falls back to the stdout sentinel, then to
        # a diagnosed error — so interleaved logs / OOM can't be mistaken for success.
        file_result = _read_container_json(container, "/output/result.json")
        if file_result is not None:
            return file_result
        if result_json is not None:
            return result_json
        if exit_info.get("OOMKilled"):
            return {"status": "error", "item_id": item_id, "error": "sandbox out of memory (OOM)"}
        if exit_code != 0:
            return {"status": "error", "item_id": item_id,
                    "error": f"sandbox exited with code {exit_code}"}
        return {"status": "no_changes", "item_id": item_id, "error": "no result produced"}
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


def _looks_rate_limited_text(s: str) -> bool:
    """True if an error string reflects a provider 429 / rate-limit (used to arm the
    circuit breaker on the coder path)."""
    t = (s or "").lower()
    return ("429" in t or "rate-limit" in t or "rate limit" in t
            or "ratelimit" in t or "temporarily rate" in t)


async def _run_coding_agent(item_id: str, title: str, description: str, story_prompt: str = "") -> None:
    """Run the Coding Agent in a thread; update state and PR URL on completion."""
    # Cost backstop — leave the story in ready so it resumes when budget frees up
    cfg = get_config(_redis_or_503())
    # Runtime config wins over the env default so the coder model is switchable live from
    # the Config tab (the sandbox otherwise reads only MODEL_CODER from the container env).
    coder_model = cfg.models.coder or _os.environ.get("MODEL_CODER", "")
    # Remember which coder model built this story so its eventual outcome (merged /
    # abandoned / escalated) can be attributed to the right model in the Metrics tab.
    if _redis_conn and coder_model:
        try:
            _redis_conn.setex(f"coder_model:{item_id}", 30 * 86_400, coder_model)
        except Exception:
            pass
    # Rate-limit hold — if the coder model is paused (429), leave the story ready and
    # come back when the breaker clears, rather than burning a doomed run.
    from event_bus import ratelimit
    if coder_model and ratelimit.is_tripped(_redis_conn, coder_model):
        log.info("coder_rate_limit_hold", id=item_id, model=coder_model)
        update_state(item_id, "ready")
        return
    if over_budget(_redis_conn, cfg.limits.max_cost_usd_daily):
        log.warning("coder_skipped_cost_cap", id=item_id)
        update_state(item_id, "ready")
        return
    # Free-tier backstop (opt-in): don't start new stories once the OpenRouter free
    # daily cap is hit — a new PR would just fail its reviewer/tester/security verdicts
    # on 429. Leave it in ready to resume when the quota resets (UTC midnight).
    if cfg.limits.hold_at_free_cap and free_quota_exceeded(_redis_conn, cfg.limits.max_free_requests_daily):
        log.warning("coder_held_free_cap", id=item_id)
        update_state(item_id, "ready")
        return
    # Enforce concurrency cap — leave story queued in ready if full
    if not _coder_slot_acquire():
        log.info("coder_cap_hit", id=item_id, cap=settings.max_coding_agents)
        update_state(item_id, "ready")
        return

    # Prefer the explicit repo field (set by planner) over the description directive
    repo = get_repo_for_story(item_id, default=settings.default_repo)
    if repo and f"repo: {repo}" not in (description or ""):
        description = f"repo: {repo}\n{description}"
    # Resolve the story's stack/SDLC → augment the prompt (5.3) + pick the image (5.2)
    stack, sdlc = _coder_context(item_id)
    guides = get_catalog().get_style_guides(get_style_guides_for_story(item_id))
    story_prompt = _augment_coder_prompt(story_prompt, stack, sdlc, guides)
    log_cb = _make_log_cb(item_id)
    log.info("coding_agent_dispatch", id=item_id, mode=settings.sandbox_mode,
             stack=stack.id, coder_image=stack.coder_image)
    import time as _timing
    _coder_t0 = _timing.monotonic()
    try:
        if settings.sandbox_mode == "docker":
            result = await asyncio.to_thread(
                _run_coding_agent_sandboxed_sync,
                item_id, title, description, story_prompt, log_cb, stack.coder_image,
                stack.test_command, stack.install_command, coder_model,
            )
        else:
            try:
                from coding_agent.main import run_coding_agent
            except ImportError:
                log.error("coding_agent_not_installed")
                update_state(item_id, "ready")
                _coder_slot_release_and_dispatch()
                return
            result = await asyncio.to_thread(run_coding_agent, item_id, title, description,
                                             story_prompt=story_prompt,
                                             test_command=stack.test_command,
                                             install_command=stack.install_command, log_line=log_cb)
    except Exception as exc:
        log.error("coding_agent_failed", id=item_id, error=str(exc))
        # A clear provider 429 (opencode raised on it) arms the breaker so the fleet
        # pauses instead of hammering a rate-limited model.
        if coder_model and _looks_rate_limited_text(str(exc)):
            ratelimit.trip(_redis_conn, coder_model)
        update_state(item_id, "ready")  # unclaim so it can be retried / resumed
        _coder_slot_release_and_dispatch()
        return
    finally:
        _expire_log(item_id)

    _err_key = f"coder_errors:{item_id}"
    if result.get("status") == "success":
        update_state(item_id, "in-review")
        if result.get("pr_url"):
            set_pr_url(item_id, result["pr_url"])
        if _redis_conn:
            try: _redis_conn.delete(_err_key)   # clear the transient-error streak
            except Exception: pass
        ratelimit.clear(_redis_conn, coder_model)   # the model is healthy
        log.info("coding_agent_complete", id=item_id, pr=result.get("pr_url"))
    elif result.get("status") == "error":
        # Agent crashed / the model failed (e.g. rate-limit) — retry, but cap the streak so
        # a persistently-failing story is flagged for a human instead of looping forever.
        log.error("coding_agent_error", id=item_id, error=result.get("error", "unknown"))
        n = 0
        if _redis_conn:
            try:
                n = _redis_conn.incr(_err_key); _redis_conn.expire(_err_key, 6 * 3600)
            except Exception:
                n = 0
        if n >= _CODER_ERROR_CAP:
            log.error("coder_error_cap_reached", id=item_id, attempts=n)
            update_state(item_id, "changes-requested")
            if _redis_conn:
                try: _redis_conn.setex(f"stuck:{item_id}", 86_400, b"1")  # attention tray
                except Exception: pass
            _record_coder_outcome(item_id, "abandoned")
        else:
            update_state(item_id, "ready")   # re-queue for another attempt
    else:
        # Agent ran and genuinely found nothing to implement — mark done, advance sequence
        log.warning("coding_agent_no_changes", id=item_id)
        if _redis_conn:
            try: _redis_conn.delete(_err_key)
            except Exception: pass
        ratelimit.clear(_redis_conn, coder_model)   # the model is healthy
        update_state(item_id, "done")
        unlock_next_story(item_id)  # backlog → ready; release+dispatch below will trigger it

    # Record coder usage. opencode runs as a subprocess and doesn't report exact token
    # counts, so cost is an ESTIMATE from the prompt + captured output priced at the
    # model's OpenRouter rate (free models → $0). Better than the previous $0/calls-only.
    _record_coder_usage(coder_model or "opencode", story_prompt, description, result, stack.id)
    if _redis_conn and coder_model:
        try:
            from event_bus.outcomes import record_latency
            record_latency(_redis_conn, "coder", coder_model, (_timing.monotonic() - _coder_t0) * 1000)
        except Exception:
            pass

    # Release slot and trigger next queued story (after all state updates are done)
    _coder_slot_release_and_dispatch()


def _record_coder_usage(model: str, story_prompt: str, description: str,
                        result: dict, stack: str = "") -> None:
    """Record coder token/cost telemetry for one opencode run.

    opencode is a subprocess that doesn't surface exact token counts, so this is a
    best-effort ESTIMATE: tokens ≈ chars/4 over the prompt sent and the captured output,
    priced at the model's OpenRouter per-token rate (free models → $0). It under-counts
    opencode's internal multi-turn context, but it makes coder spend visible in telemetry
    and feed the daily cost cap instead of silently reporting $0. Written in the same
    telemetry:llm:{date} schema as the litellm-backed roles so it aggregates uniformly."""
    if _redis_conn is None:
        return
    try:
        in_tok = (len(story_prompt or "") + len(description or "")) // 4
        out_tok = len(str((result or {}).get("summary") or "")) // 4
        cost = 0.0
        try:
            from event_bus.models_catalog import get_model_meta
            meta = get_model_meta(_redis_conn, model) or {}
            cost = (in_tok * float(meta.get("price_prompt") or 0.0)
                    + out_tok * float(meta.get("price_completion") or 0.0))
        except Exception:
            pass
        import time
        date = time.strftime("%Y-%m-%d", time.gmtime())
        key = f"telemetry:llm:{date}"
        prefix = f"coder:{model}"
        pipe = _redis_conn.pipeline()
        pipe.hincrbyfloat(key, f"{prefix}:cost_usd", cost)
        pipe.hincrby(key, f"{prefix}:input_tokens", in_tok)
        pipe.hincrby(key, f"{prefix}:output_tokens", out_tok)
        pipe.hincrby(key, f"{prefix}:calls", 1)
        pipe.expire(key, 30 * 86_400)
        if stack:
            skey = f"telemetry:stack:{date}"
            pipe.hincrbyfloat(skey, f"{stack}:cost_usd", cost)
            pipe.hincrby(skey, f"{stack}:input_tokens", in_tok)
            pipe.hincrby(skey, f"{stack}:output_tokens", out_tok)
            pipe.hincrby(skey, f"{stack}:calls", 1)
            pipe.expire(skey, 30 * 86_400)
        pipe.execute()
    except Exception:
        pass


def _record_coder_outcome(item_id: str, outcome: str) -> None:
    """Attribute a story's terminal outcome (merged/abandoned/escalated/first_pass) to
    the coder model that built it, for the Metrics tab. Best-effort."""
    if _redis_conn is None:
        return
    try:
        m = _redis_conn.get(f"coder_model:{item_id}")
        model = m.decode() if isinstance(m, bytes) else m
        if not model:
            return
        from event_bus.outcomes import record_outcome
        record_outcome(_redis_conn, "coder", model, outcome)
    except Exception:
        pass


def _mark_reworked(item_id: str) -> None:
    """Flag a story as having needed a recode/escalation, so it doesn't count as
    first-pass when it eventually merges."""
    if _redis_conn is None:
        return
    try:
        _redis_conn.setex(f"story_reworked:{item_id}", 30 * 86_400, b"1")
    except Exception:
        pass


def _post_pr_comment(repo_full_name: str, pr_number: int, body: str) -> bool:
    """Best-effort PR comment (used to surface a stuck recode to the operator)."""
    try:
        from coding_agent.forgejo_client import ForgejoClient
        from coding_agent.config import settings as cs
        owner, repo_name = repo_full_name.split("/", 1)
        with ForgejoClient(cs.forgejo_base_url, cs.forgejo_api_token) as fg:
            fg.post_pr_comment(owner, repo_name, pr_number, body)
        return True
    except Exception as exc:
        log.warning("pr_comment_failed", repo=repo_full_name, pr=pr_number, error=str(exc))
        return False


def _flag_recode_stuck(repo_full_name: str, pr_number: int, item_id: str,
                       status: str, attempt: int) -> None:
    """A recode produced no fix — surface it instead of silently parking the story.

    Re-running with identical feedback would no-op again, so we leave the story in
    changes-requested and post a PR comment asking for human attention.
    """
    log.warning("recode_stuck", item_id=item_id, pr=pr_number, status=status, attempt=attempt)
    _post_pr_comment(
        repo_full_name, pr_number,
        "🤖 **Auto-fix couldn't resolve the failing checks.** The coding agent re-ran "
        f"(attempt {attempt}) but produced no changes, so this PR is parked in "
        "`changes-requested` and needs human attention — review the failing CI/checks and "
        "push a fix, or close the PR.",
    )
    update_state(item_id, "changes-requested")


async def _run_recode_agent(review_event: ForgejoReviewEvent) -> None:
    """Fetch review comments and re-run the coding agent on the existing PR branch."""
    pr_url = review_event.pr_html_url
    item = find_item_by_pr_url(pr_url)
    if not item:
        log.warning("recode_no_story_found", pr_url=pr_url)
        return

    item_id = item["id"]
    update_state(item_id, "changes-requested")
    log.info("recode_agent_queued", item_id=item_id, pr=review_event.pr_number)

    # Fetch review comments from Forgejo API
    review_comments: list[dict] = []
    try:
        from coding_agent.forgejo_client import ForgejoClient
        from coding_agent.config import settings as coding_settings
        owner, repo_name = review_event.repo_full_name.split("/", 1)
        with ForgejoClient(coding_settings.forgejo_base_url, coding_settings.forgejo_api_token) as fg:
            # General review body
            if review_event.review.body.strip():
                review_comments.append({"path": "", "body": review_event.review.body})
            # Inline comments on the review
            inline = fg.get(f"/repos/{owner}/{repo_name}/pulls/{review_event.pr_number}/reviews/{review_event.review.id}/comments")
            for c in inline:
                if c.get("body", "").strip():
                    review_comments.append({"path": c.get("path", ""), "body": c["body"]})
    except Exception as exc:
        log.warning("recode_comment_fetch_failed", error=str(exc))
        if review_event.review.body.strip():
            review_comments = [{"path": "", "body": review_event.review.body}]

    review_fix_prompt = get_prompt(_redis_or_503(), "coder.review_fix")
    log_cb = _make_log_cb(item_id)
    try:
        from coding_agent.main import fix_pr_review
        result = await asyncio.to_thread(
            fix_pr_review,
            item["id"], item["title"], item.get("description", ""),
            review_event.head_ref, review_event.repo_full_name,
            review_comments,
            review_fix_prompt=review_fix_prompt,
            log_line=log_cb,
        )
    except Exception as exc:
        log.error("recode_agent_failed", item_id=item_id, error=str(exc))
        _flag_recode_stuck(review_event.repo_full_name, review_event.pr_number,
                           item_id, "error", 1)
        return
    finally:
        _expire_log(item_id)

    if result.get("status") == "success":
        update_state(item_id, "in-review")
        log.info("recode_agent_complete", item_id=item_id, sha=result.get("sha", "")[:8])
    else:
        # opencode made no changes — the recode can't help; flag for a human.
        _flag_recode_stuck(review_event.repo_full_name, review_event.pr_number,
                           item_id, result.get("status", "no_changes"), 1)


_MAX_RECODE_RETRIES = 3
# Stronger model coder+reviewer escalate to when a story stalls and models.escalate is unset.
# NOTE: opencode's registry uses dot notation (claude-sonnet-4.6) and litellm accepts it too,
# so this one string resolves for BOTH the coder (opencode) and reviewer (litellm). The dash
# form ("...-4-6") only resolves in litellm, so the coder's escalation silently failed with
# ProviderModelNotFoundError. (The coder also falls back to the base model if an escalate
# model is ever unresolvable — see opencode_agent.fallback_model.)
_DEFAULT_ESCALATE_MODEL = "openrouter/anthropic/claude-sonnet-4.6"


def _recode_cap_for_item(item: dict) -> int:
    """Resolve the recode cap for a story: the stack's ``recode_cap`` if it sets a
    positive one, else the global default. Lets stricter-CI stacks (e.g. Rust) get
    more auto-fix attempts before a PR is parked for a human."""
    try:
        stack = get_catalog().get_stack(item.get("stack") or "")
        cap = getattr(stack, "recode_cap", 0) or 0
        if cap > 0:
            return cap
    except Exception as exc:  # noqa: BLE001 — never block a recode on catalog issues
        log.warning("recode_cap_resolve_failed", error=str(exc)[:120])
    return _MAX_RECODE_RETRIES


# ── Post-merge CI gate: merged is transient; CI on main decides done vs fix ────
_POST_MERGE_CI_TIMEOUT = 600   # seconds to wait for CI on the merge commit
_POST_MERGE_CI_GRACE = 60      # if no status appears by now, treat as "no CI workflow"
_POST_MERGE_FIX_CAP = 2        # automated fix attempts before leaving it for a human


def _get_branch_head(repo_full_name: str, branch: str = "main") -> str:
    from coding_agent.forgejo_client import ForgejoClient
    from coding_agent.config import settings as cs
    owner, repo = repo_full_name.split("/", 1)
    with ForgejoClient(cs.forgejo_base_url, cs.forgejo_api_token) as fj:
        data = fj.get(f"/repos/{owner}/{repo}/branches/{branch}")
    return (data.get("commit") or {}).get("id", "")


def _poll_commit_ci(repo_full_name: str, sha: str,
                    timeout: int = _POST_MERGE_CI_TIMEOUT,
                    interval: int = 10, grace: int = _POST_MERGE_CI_GRACE) -> str:
    """Poll a commit's CI status. Returns 'success' | 'failure' | 'none' | 'timeout'."""
    import time as _t
    from coding_agent.forgejo_client import ForgejoClient
    from coding_agent.config import settings as cs
    owner, repo = repo_full_name.split("/", 1)
    start = _t.time()
    with ForgejoClient(cs.forgejo_base_url, cs.forgejo_api_token) as fj:
        while True:
            try:
                st = fj.get(f"/repos/{owner}/{repo}/commits/{sha}/status")
            except Exception:
                st = {}
            state = st.get("state") or ""
            statuses = st.get("statuses") or []
            elapsed = _t.time() - start
            if statuses:
                if state == "success":
                    return "success"
                if state in ("failure", "error"):
                    return "failure"
            elif elapsed >= grace:
                return "none"  # no CI workflow reported a status
            if elapsed >= timeout:
                return "timeout"
            _t.sleep(interval)


def _ci_cancelled(repo_full_name: str, sha: str) -> bool:
    """True if the Actions run for `sha` was cancelled (superseded by a newer merge to
    main) rather than a genuine test failure. Forgejo's commit-status API reports a
    cancelled run as 'failure', so we consult the Actions API — which exposes
    status=='cancelled' — to tell a supersede apart from a real red build."""
    from coding_agent.forgejo_client import ForgejoClient
    from coding_agent.config import settings as cs
    owner, repo = repo_full_name.split("/", 1)
    try:
        with ForgejoClient(cs.forgejo_base_url, cs.forgejo_api_token) as fj:
            data = fj.get(f"/repos/{owner}/{repo}/actions/tasks?limit=40")
        runs = [t for t in (data.get("workflow_runs") or []) if t.get("head_sha") == sha]
        # cancelled iff there is a run for this sha and none of them genuinely
        # succeeded/failed (all were cancelled/superseded).
        return bool(runs) and all(t.get("status") == "cancelled" for t in runs)
    except Exception:
        return False


def _advance_after_done(item_id: str) -> None:
    """Unlock + dispatch the next sequenced story once item_id is done."""
    unlocked = unlock_next_story(item_id)
    if unlocked:
        update_state(unlocked["id"], "in-progress")
        story_prompt = get_prompt(_redis_conn, "coder.story")
        asyncio.create_task(_run_coding_agent(
            unlocked["id"], unlocked["title"], unlocked.get("description") or "", story_prompt,
        ))
        log.info("coding_agent_auto_triggered", story_id=unlocked["id"])


def _post_merge_fix(item_id: str, ci_result: str) -> None:
    """Post-merge CI failed on main — return the story to a developer (capped auto-fix)."""
    item = get_item(item_id) or {}
    cap_key = f"post_merge_fix:{item_id}"
    attempts = int((_redis_conn.get(cap_key) or 0)) if _redis_conn else 0
    if attempts >= _POST_MERGE_FIX_CAP:
        log.warning("post_merge_fix_cap_reached", item_id=item_id, attempts=attempts)
        return  # leave it in changes-requested for a human
    if _redis_conn:
        _redis_conn.setex(cap_key, 86400, attempts + 1)
    update_state(item_id, "in-progress")
    story_prompt = get_prompt(_redis_conn, "coder.story")
    desc = (item.get("description") or "") + (
        f"\n\nNOTE: after merge, CI on `main` is failing ({ci_result}). Open a fix that "
        "makes the test suite pass on main."
    )
    asyncio.create_task(_run_coding_agent(item_id, item.get("title", ""), desc, story_prompt))
    log.info("post_merge_fix_dispatched", item_id=item_id, attempt=attempts + 1)


async def _await_post_merge_ci(item_id: str, repo_full_name: str) -> None:
    """After a PR merges, run/observe CI on main: success → done + advance; fail → back to dev."""
    if not repo_full_name:
        # No repo context to verify — don't strand the story.
        update_state(item_id, "done")
        _advance_after_done(item_id)
        return
    sha = await asyncio.to_thread(_get_branch_head, repo_full_name, "main")
    result = await asyncio.to_thread(_poll_commit_ci, repo_full_name, sha)
    item = get_item(item_id)
    if not item or item.get("state") != "merged":
        return  # state changed elsewhere; don't clobber
    if result in ("success", "none"):
        update_state(item_id, "done")
        log.info("post_merge_ci_passed", item_id=item_id, ci=result, sha=sha[:8])
        _advance_after_done(item_id)
    elif result == "failure" and await asyncio.to_thread(_ci_cancelled, repo_full_name, sha):
        # The run was cancelled by a superseding merge, not a real failure — the story's
        # code is already on main and a later merge's CI covers the cumulative state.
        # Bouncing it to changes-requested would pointlessly re-code merged code.
        update_state(item_id, "done")
        log.info("post_merge_ci_cancelled_advance", item_id=item_id, sha=sha[:8])
        _advance_after_done(item_id)
    else:
        update_state(item_id, "changes-requested")
        log.warning("post_merge_ci_failed", item_id=item_id, ci=result, sha=sha[:8])
        _post_merge_fix(item_id, result)


def _on_story_merged(item_id: str, repo_full_name: str = "") -> dict | None:
    """A PR merged → 'merged' is transient; post-merge CI on main decides done vs fix.
    The next story is NOT unlocked until this one reaches 'done' (CI-verified main)."""
    updated = update_state(item_id, "merged")
    # Coder success: the PR was good enough to merge. Credit first_pass only if the
    # story never needed a recode/escalation on its way here.
    _record_coder_outcome(item_id, "merged")
    if _redis_conn and not _redis_conn.get(f"story_reworked:{item_id}"):
        _record_coder_outcome(item_id, "first_pass")
    repo = repo_full_name or get_repo_for_story(item_id)
    asyncio.create_task(_await_post_merge_ci(item_id, repo))
    return updated


@app.post("/internal/pr-merged", status_code=status.HTTP_200_OK)
async def internal_pr_merged(payload: dict):
    """Called by the reviewer gate after auto-merging a PR via the API."""
    pr_url = payload.get("pr_url", "")
    pr_number = payload.get("pr_number", 0)
    item = find_item_by_pr_url(pr_url) if pr_url else None
    if not item:
        raise HTTPException(status_code=404, detail="no story found for this PR")
    updated = _on_story_merged(item["id"], payload.get("repo_full_name", ""))
    log.info("internal_pr_merged", item_id=item["id"], pr=pr_number)
    return {"merged": updated, "post_merge_ci": "pending"}


@app.post("/internal/recode-for-pr", status_code=status.HTTP_202_ACCEPTED)
async def internal_recode_for_pr(payload: dict):
    """
    Called by the reviewer gate (worker) when checks fail.
    Triggers the recode agent without going through the Forgejo webhook chain.
    Capped per PR (stack's recode_cap, else the global default) to break loops.
    """
    repo_full_name = payload.get("repo_full_name", "")
    pr_number = payload.get("pr_number", 0)
    pr_url = payload.get("pr_url", "")
    feedback = payload.get("feedback", "Automated checks failed — see PR comments for details.")

    item = find_item_by_pr_url(pr_url) if pr_url else None
    if not item:
        raise HTTPException(status_code=404, detail="no story found for this PR")

    # Retry cap — prevent infinite recode loops (per-stack; Rust et al. can raise it)
    r = _redis_or_503()
    cap = _recode_cap_for_item(item)
    retry_key = f"recode_retries:{repo_full_name}:{pr_number}"
    esc_key = f"escalate_pr:{repo_full_name}:{pr_number}"
    retries = int(r.get(retry_key) or 0)
    if retries >= cap:
        cfg = get_config(r)
        # Cascade: escalate coder+reviewer to a stronger model for one more round before
        # handing off to a human. esc_key both marks "already escalated" and holds the
        # override model both the coder (below) and the reviewer fan-out read.
        if cfg.gates.auto_escalate and not r.get(esc_key):
            esc_model = (cfg.models.escalate or "").strip() or _DEFAULT_ESCALATE_MODEL
            r.setex(esc_key, 7 * 86_400, esc_model)
            r.delete(retry_key)                    # fresh recode cap under the stronger model
            r.delete(f"stuck:{item['id']}")        # actively retrying — not stuck (yet)
            retries = 0
            _post_pr_comment(
                repo_full_name, pr_number,
                f"⬆️ **Auto-escalating to a stronger model** (`{esc_model}`) after {cap} failed "
                "attempts — retrying the coder and re-reviewing on the next push.")
            log.warning("story_escalated", item_id=item["id"], pr=pr_number, model=esc_model)
            _record_coder_outcome(item["id"], "escalated")
            _mark_reworked(item["id"])
            # fall through: run the recode now with the escalated coder model
        else:
            escalated = bool(r.get(esc_key))
            note = " even after escalating to a stronger model" if escalated else ""
            log.warning("recode_retry_cap_reached", pr=pr_number, repo=repo_full_name,
                        retries=retries, cap=cap, escalated=escalated)
            _post_pr_comment(
                repo_full_name, pr_number,
                f"🤖 **Auto-fix gave up after {retries} attempts{note}.** The checks are still "
                "failing — this PR needs human attention.")
            update_state(item["id"], "changes-requested")
            r.setex(f"stuck:{item['id']}", 86_400, b"1")   # surface in the attention tray
            _record_coder_outcome(item["id"], "abandoned")
            return {"status": "retry_cap_reached", "retries": retries,
                    "item_id": item["id"], "escalated": escalated}
    _mark_reworked(item["id"])   # any recode means this story is no longer first-pass
    r.setex(retry_key, 86400, retries + 1)

    owner, repo_name = repo_full_name.split("/", 1)
    review_comments = [{"path": "", "body": feedback}]

    # Pull inline PR comments from Forgejo for richer context
    try:
        from coding_agent.forgejo_client import ForgejoClient as CodingFJ
        from coding_agent.config import settings as cs
        with CodingFJ(cs.forgejo_base_url, cs.forgejo_api_token) as fg:
            for c in fg.get(f"/repos/{owner}/{repo_name}/issues/{pr_number}/comments"):
                body = c.get("body", "").strip()
                if body:
                    review_comments.append({"path": "", "body": body})
    except Exception as exc:
        log.warning("recode_comment_fetch_failed", error=str(exc))

    log.info("internal_recode_triggered", item_id=item["id"], pr=pr_number,
             repo=repo_full_name, attempt=retries + 1)
    review_fix_prompt = get_prompt(r, "coder.review_fix")
    log_cb = _make_log_cb(item["id"])

    esc_coder_model = (r.get(esc_key) or b"").decode() if r.get(esc_key) else ""

    async def _recode_task() -> None:
        from coding_agent.main import fix_pr_review
        result: dict = {}
        try:
            result = await asyncio.to_thread(
                fix_pr_review,
                item["id"], item["title"], item.get("description", ""),
                payload.get("head_ref", ""), repo_full_name,
                review_comments,
                review_fix_prompt=review_fix_prompt,
                log_line=log_cb,
                model_override=esc_coder_model,   # stronger coder when escalated
            ) or {}
        except Exception as exc:
            log.error("recode_agent_failed", item_id=item["id"], error=str(exc))
            result = {"status": "error"}
        finally:
            _expire_log(item["id"])

        if result.get("status") == "success":
            # A fix was pushed; the synchronized webhook re-runs review/CI.
            update_state(item["id"], "in-review")
            log.info("recode_pushed_fix", item_id=item["id"], sha=result.get("sha", "")[:8])
        else:
            # No changes / error — recode can't help; surface it for a human.
            _flag_recode_stuck(repo_full_name, pr_number, item["id"],
                               result.get("status", "no_changes"), retries + 1)

    asyncio.create_task(_recode_task())
    update_state(item["id"], "changes-requested")
    return {"status": "recode_started", "item_id": item["id"], "attempt": retries + 1}


@app.post("/api/items/{item_id}/plan", status_code=status.HTTP_202_ACCEPTED)
async def plan_item(item_id: str):
    """(Re-)run the Planner Agent for any approved idea, creating/recreating its stories."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["type"] != "idea":
        raise HTTPException(status_code=409, detail="only ideas can be planned")
    asyncio.create_task(_run_planner(item_id, item["title"], item["description"] or ""))
    return {"status": "planning_started", "id": item_id}


@app.post("/api/items/{item_id}/approve-plan", status_code=status.HTTP_202_ACCEPTED)
async def approve_plan(item_id: str):
    """Release a plan held by the plan-approval gate: start the first story running."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["type"] != "idea":
        raise HTTPException(status_code=409, detail="only projects have a plan to approve")
    stories = [s for s in list_items(item_type="story") if s.get("parent_id") == item_id]
    if not stories:
        raise HTTPException(status_code=409, detail="no plan to start — run the planner first")
    if any(s["state"] != "backlog" for s in stories):
        raise HTTPException(status_code=409, detail="plan already started")
    first = min(stories, key=lambda s: s.get("sequence") or 999)
    update_state(first["id"], "in-progress")
    story_prompt = get_prompt(_redis_or_503(), "coder.story")
    asyncio.create_task(_run_coding_agent(
        first["id"], first["title"], first.get("description") or "", story_prompt))
    log.info("plan_approved", idea_id=item_id, stories=len(stories), first=first["id"])
    return {"status": "plan_started", "id": item_id, "first_story": first["id"]}


@app.post("/api/items/{item_id}/reject", status_code=status.HTTP_200_OK)
async def reject_item(item_id: str):
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["state"] != "pending-approval":
        raise HTTPException(status_code=409, detail=f"item is '{item['state']}', not pending-approval")
    updated = update_state(item_id, "rejected")
    log.info("idea_rejected", id=item_id, title=item["title"])
    return updated


# ── Operator recovery controls ──────────────────────────────────────────────
# Guarded actions to un-stick work without hand-editing SQLite/Redis: retry a
# parked PR, reset a wedged coder, abandon a story, or cancel a whole idea.

_RETRYABLE = ("changes-requested", "in-review")
_RESETTABLE = ("in-progress", "in-review", "changes-requested")


@app.post("/api/items/{item_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_item(item_id: str):
    """Parked story → clear the recode/CI caps and re-run review+tests+CI on the PR's
    current head (a failing verdict then recodes with a fresh cap)."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["type"] != "story":
        raise HTTPException(status_code=409, detail="only stories can be retried")
    if item["state"] not in _RETRYABLE:
        raise HTTPException(status_code=409, detail=f"story is '{item['state']}', not retryable")
    parts = _pr_url_parts(item.get("pr_url") or "")
    if not parts:
        raise HTTPException(status_code=409, detail="story has no PR to retry; use reset instead")
    repo_full_name, pr_number = parts
    owner, repo_name = repo_full_name.split("/", 1)
    _clear_recovery_keys(item)
    _clear_watchdog_state(item_id)
    try:
        from coding_agent.forgejo_client import ForgejoClient as CFJ
        from coding_agent.config import settings as cs
        with CFJ(cs.forgejo_base_url, cs.forgejo_api_token) as fg:
            pr = fg.get(f"/repos/{owner}/{repo_name}/pulls/{pr_number}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"could not fetch PR: {exc}")
    head = pr.get("head") or {}
    from event_bus.jobs.handlers import handle_pr_event
    _queue_or_503().enqueue(
        handle_pr_event, repo_full_name=repo_full_name, pr_number=pr_number,
        head_sha=head.get("sha", ""), action="synchronized",
        head_ref=head.get("ref", ""), base_ref=(pr.get("base") or {}).get("ref", "main"),
    )
    update_state(item_id, "in-review")
    _post_pr_comment(repo_full_name, pr_number,
                     "🔁 **Operator retry** — caps cleared; re-running review, tests, and CI on the current head.")
    log.info("operator_retry", id=item_id, repo=repo_full_name, pr=pr_number)
    return {"status": "retry_requeued", "id": item_id}


@app.post("/api/items/{item_id}/reset", status_code=status.HTTP_202_ACCEPTED)
async def reset_item(item_id: str):
    """Wedged story (e.g. its coder task died on a restart) → back to a fresh coding
    run: clear caps, claim it, and re-dispatch the Coding Agent."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["type"] != "story":
        raise HTTPException(status_code=409, detail="only stories can be reset")
    if item["state"] not in _RESETTABLE:
        raise HTTPException(status_code=409, detail=f"story is '{item['state']}', not resettable")
    _clear_recovery_keys(item)
    _clear_watchdog_state(item_id)
    update_state(item_id, "in-progress")
    story_prompt = get_prompt(_redis_or_503(), "coder.story")
    asyncio.create_task(_run_coding_agent(item_id, item["title"], item.get("description") or "", story_prompt))
    log.info("operator_reset", id=item_id, prev=item["state"])
    return {"status": "reset_and_coding", "id": item_id}


@app.post("/api/items/{item_id}/abandon", status_code=status.HTTP_200_OK)
async def abandon_item(item_id: str):
    """Give up on a story: mark it abandoned and unlock the next so the rest of the
    project isn't blocked."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["type"] != "story":
        raise HTTPException(status_code=409, detail="only stories can be abandoned")
    if item["state"] in ("done", "abandoned"):
        raise HTTPException(status_code=409, detail=f"story is already '{item['state']}'")
    _record_coder_outcome(item_id, "abandoned")   # before clearing coder attribution
    _clear_recovery_keys(item)
    updated = update_state(item_id, "abandoned")
    unlock_next_story(item_id)
    _dispatch_next_ready()
    log.info("operator_abandon_story", id=item_id, title=item["title"])
    return updated


@app.post("/api/items/{item_id}/cancel", status_code=status.HTTP_200_OK)
async def cancel_idea(item_id: str):
    """Stop a whole idea: abandon its non-terminal stories and mark the idea abandoned."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["type"] != "idea":
        raise HTTPException(status_code=409, detail="only ideas can be cancelled")
    stopped = 0
    for child in list_items(item_type="story"):
        if child.get("parent_id") == item_id and child["state"] not in ("done", "abandoned", "rejected"):
            _clear_recovery_keys(child)
            update_state(child["id"], "abandoned")
            stopped += 1
    updated = update_state(item_id, "abandoned")
    log.info("operator_cancel_idea", id=item_id, stories_stopped=stopped)
    return updated


# ── Project lifecycle: archive / restore / delete ───────────────────────────
# A project may be archived or deleted only once it's settled — no story is still
# in-flight. That both keeps the board honest and, for repo deletion, guarantees CI
# has drained so deleting the Forgejo repo can't wedge the runner.

_IN_FLIGHT = ("in-progress", "in-review", "changes-requested", "merged")


def _project_has_in_flight(idea_id: str) -> bool:
    return any(s.get("parent_id") == idea_id and s["state"] in _IN_FLIGHT
              for s in list_items(item_type="story"))


def _delete_forgejo_repo(repo_full_name: str) -> bool:
    """Delete a Forgejo repo via the admin API (internal URL). Returns True on success
    or if it's already gone."""
    import httpx
    from coding_agent.config import settings as cs
    try:
        owner, repo = repo_full_name.split("/", 1)
    except ValueError:
        return False
    try:
        with httpx.Client(timeout=15) as c:
            r = c.delete(f"{cs.forgejo_base_url}/api/v1/repos/{owner}/{repo}",
                         headers={"Authorization": f"token {cs.forgejo_api_token}"})
        log.info("forgejo_repo_deleted", repo=repo_full_name, status=r.status_code)
        return r.status_code in (204, 404)
    except Exception as exc:  # noqa: BLE001
        log.error("forgejo_repo_delete_failed", repo=repo_full_name, error=str(exc))
        return False


def _require_settled_project(item_id: str) -> dict:
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["type"] != "idea":
        raise HTTPException(status_code=409, detail="only projects can be archived/deleted")
    if _project_has_in_flight(item_id):
        raise HTTPException(status_code=409,
                            detail="project has active work — cancel it first, then archive/delete")
    return item


@app.get("/api/projects")
async def list_projects_endpoint():
    """All projects (active + archived) with a story-progress summary."""
    return {"projects": list_projects()}


@app.post("/api/items/{item_id}/archive", status_code=status.HTTP_200_OK)
async def archive_project(item_id: str):
    """Hide a settled project (and its stories) from the board; reversible via restore."""
    _require_settled_project(item_id)
    n = set_archived(item_id, True)
    log.info("project_archived", id=item_id, items=n)
    return {"status": "archived", "id": item_id, "items": n}


@app.post("/api/items/{item_id}/unarchive", status_code=status.HTTP_200_OK)
async def unarchive_project(item_id: str):
    """Restore an archived project back onto the board."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["type"] != "idea":
        raise HTTPException(status_code=409, detail="only projects can be restored")
    n = set_archived(item_id, False)
    log.info("project_restored", id=item_id, items=n)
    return {"status": "restored", "id": item_id, "items": n}


@app.delete("/api/items/{item_id}", status_code=status.HTTP_200_OK)
async def delete_project(item_id: str, delete_repo: bool = False):
    """Permanently delete a settled project (idea + stories). With delete_repo=true,
    also delete its Forgejo repo (safe: settled ⇒ CI has drained)."""
    item = _require_settled_project(item_id)
    repo_deleted = None
    repo = item.get("repo")
    if delete_repo and repo:
        repo_deleted = _delete_forgejo_repo(repo)
    n = delete_item_tree(item_id)
    log.info("project_deleted", id=item_id, items=n, repo=repo if delete_repo else None,
             repo_deleted=repo_deleted)
    return {"status": "deleted", "id": item_id, "items_deleted": n, "repo_deleted": repo_deleted}


@app.post("/api/items/{item_id}/merged", status_code=status.HTTP_200_OK)
async def mark_merged(item_id: str):
    """Mark a story as merged and unlock the next sequenced story."""
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["state"] not in ("in-review", "changes-requested"):
        raise HTTPException(status_code=409, detail=f"item is '{item['state']}', expected in-review")
    updated = _on_story_merged(item_id)
    log.info("story_merged", id=item_id, post_merge_ci="pending")
    return {"merged": updated, "post_merge_ci": "pending"}


# ── Runtime config API (Phase 6) ──────────────────────────────────────────────

@app.get("/api/config")
async def get_runtime_config():
    """
    Return the current runtime configuration: gate flags and per-role model overrides.

    Gates:
      idea_approval    — always true (read-only); ideas require human approval before planning
      pr_merge_approval — when true, PRs must be manually approved via POST /api/prs/.../approve
      security_signoff  — when true, PRs are blocked from merge if the security scan fails

    Models:
      Per-role litellm model strings. Empty string means use the env-var default.
      Examples: "openrouter/anthropic/claude-sonnet-4-6", "openrouter/openai/gpt-4o"
    """
    return asdict(get_config(_redis_or_503()))


@app.patch("/api/config")
async def patch_runtime_config(request: Request):
    """
    Partially update runtime configuration. Only supplied fields are changed.

    Example — switch to GPT-4o for code review and enable manual PR approval:
      {"models": {"reviewer": "openrouter/openai/gpt-4o"}, "gates": {"pr_merge_approval": true}}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    # claude-code (subscription) models are supported only by the idea/planner call
    # paths; the high-volume roles would fail their litellm calls — and must not draw
    # on the subscription anyway (weekly caps would hard-stop them).
    for role, val in (body.get("models") or {}).items():
        if str(val).strip().startswith("claude-code") and role not in ("idea", "planner"):
            raise HTTPException(
                status_code=422,
                detail=f"claude-code models are planning-only (idea/planner), not '{role}'",
            )
    return asdict(patch_config(_redis_or_503(), body))


# ── Stack & SDLC catalog API (EPIC 1) ─────────────────────────────────────────

@app.get("/api/stacks")
async def list_stacks():
    """The tech stacks the Planner can propose (used by the approval UI)."""
    cat = get_catalog()
    return {"stacks": [
        {"id": s.id, "display_name": s.display_name, "default_sdlc": s.default_sdlc}
        for s in cat.list_stacks()
    ]}


@app.get("/api/sdlc")
async def list_sdlc_styles():
    """The SDLC styles that shape story decomposition."""
    cat = get_catalog()
    return {"sdlc": [
        {"id": s.id, "display_name": s.display_name} for s in cat.list_sdlc()
    ]}


@app.get("/api/style-guides")
async def list_style_guides():
    """The code-style guides the user can apply (multi-select at approval)."""
    cat = get_catalog()
    return {"style_guides": [
        {"id": g.id, "display_name": g.display_name, "applies_to_stacks": g.applies_to_stacks}
        for g in cat.list_style_guides()
    ]}


@app.post("/api/catalog/reload", status_code=status.HTTP_200_OK)
async def reload_catalog_endpoint():
    """Re-read stack/SDLC/style-guide definitions from disk (after adding a new one)."""
    cat = reload_catalog()
    log.info("catalog_reloaded", stacks=len(cat.stacks), sdlc=len(cat.sdlc),
             style_guides=len(cat.style_guides))
    return {"stacks": len(cat.stacks), "sdlc": len(cat.sdlc),
            "style_guides": len(cat.style_guides)}


# ── Prompt management API ─────────────────────────────────────────────────────

@app.get("/api/prompts")
async def get_prompts():
    """Return all agent prompt templates with their defaults and current values."""
    return list_prompts(_redis_or_503())


@app.put("/api/prompts/{key:path}")
async def update_prompt(key: str, request: Request):
    """Save a custom prompt for the given key."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    value = body.get("value", "")
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="'value' must be a string")
    try:
        set_prompt(_redis_or_503(), key, value)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"key": key, "saved": True}


@app.delete("/api/prompts/{key:path}", status_code=status.HTTP_200_OK)
async def reset_prompt(key: str):
    """Reset a prompt to its built-in default."""
    delete_prompt(_redis_or_503(), key)
    return {"key": key, "reset": True}


# ── PR approval (Phase 6) ──────────────────────────────────────────────────────

@app.post("/api/prs/{owner}/{repo}/{pr_number}/approve", status_code=status.HTTP_202_ACCEPTED)
async def approve_pr_merge(owner: str, repo: str, pr_number: int, request: Request):
    """
    Approve a PR that is holding for human review (gate.pr_merge_approval = true).

    If TEMPORAL_ADDRESS is configured, sends an 'approve' signal to the running
    PRReviewWorkflow. Otherwise reads the Redis pending-merge key and enqueues
    a merge job directly.
    """
    body: dict = {}
    try:
        body = await request.json()
    except Exception:
        pass
    approver = body.get("approver", "human")

    # Temporal signal path
    if settings.temporal_address:
        try:
            from temporalio.client import Client
            client = await Client.connect(settings.temporal_address)
            workflow_id = f"pr-review-{owner}-{repo}-{pr_number}"
            handle = client.get_workflow_handle(workflow_id)
            await handle.signal("approve", approver)
            log.info("temporal_approval_signal_sent", owner=owner, repo=repo, pr=pr_number)
            return {"status": "approved", "via": "temporal_signal"}
        except Exception as exc:
            log.error("temporal_signal_failed", error=str(exc))
            raise HTTPException(status_code=502, detail=f"Temporal signal failed: {exc}")

    # RQ fallback: check Redis for pending merge token
    r = _redis_or_503()
    pending_key = f"pr_merge_pending:{owner}:{repo}:{pr_number}"
    if not r.exists(pending_key):
        raise HTTPException(
            status_code=404,
            detail="No pending merge for this PR — already merged, or gate.pr_merge_approval is off.",
        )

    r.delete(pending_key)
    from event_bus.jobs.pr_jobs import do_merge_pr
    job = _queue_or_503().enqueue(do_merge_pr, owner=owner, repo=repo, pr_number=pr_number)
    log.info("pr_merge_enqueued_on_approval", owner=owner, repo=repo, pr=pr_number, job_id=job.id)
    return {"status": "merging", "job_id": job.id}


# ── Board / issue browser ─────────────────────────────────────────────────────

# ── Model discovery ───────────────────────────────────────────────────────────

@app.get("/api/models/openrouter")
async def list_openrouter_models(refresh: bool = False):
    """
    List free OpenRouter models (cached 2h in Redis) plus static Ollama suggestions.

    Pass ?refresh=true to force a fresh fetch from OpenRouter regardless of cache.
    No OpenRouter API key is required to list models; set OPENROUTER_API_KEY to
    raise rate limits if you hit 429s.

    Response: {models: [...], ollama: [...], count: int, cached_at: int}
    """
    from event_bus.models_catalog import get_free_models, refresh_free_models, get_ollama_suggestions
    r = _redis_or_503()
    try:
        if refresh:
            data = refresh_free_models(r, settings.openrouter_api_key)
        else:
            data = get_free_models(r, settings.openrouter_api_key)
    except Exception as exc:
        log.error("openrouter_models_fetch_failed", error=str(exc))
        raise HTTPException(status_code=502, detail=f"Failed to fetch models from OpenRouter: {exc}")
    data["ollama"] = get_ollama_suggestions()
    return data


@app.get("/api/models/meta")
async def model_meta(refresh: bool = False):
    """Per-model metadata (context length, price/token, structured-output + tool support,
    modalities, deprecation) for the whole OpenRouter catalog — powers the model picker's
    cost/context/capability hints and the import context-fit check. Cached 2h."""
    from event_bus.models_catalog import refresh_free_models, _META_KEY
    r = _redis_or_503()
    raw = r.get(_META_KEY)
    if raw is None or refresh:
        try:
            await asyncio.to_thread(refresh_free_models, r, settings.openrouter_api_key)
            raw = r.get(_META_KEY)
        except Exception as exc:
            log.warning("model_meta_fetch_failed", error=str(exc)[:120])
    return {"meta": _json.loads(raw) if raw else {}}


# ── Telemetry (Phase 7) ────────────────────────────────────────────────────────

@app.get("/api/telemetry")
async def get_telemetry(days: int = 7):
    """
    Return LLM cost/token telemetry, current concurrency, and rate-limit rejection
    counts aggregated over the last `days` days (default: 7), plus today's OpenRouter
    free-tier request usage vs the configured daily cap.
    """
    r = _redis_or_503()
    summary = get_telemetry_summary(r, days=days)
    cfg = get_config(r)
    summary["free_quota"] = free_quota_status(r, cfg.limits.max_free_requests_daily)
    return summary


_ROLE_ORDER = {"coder": 0, "reviewer": 1, "tester": 2, "security": 3, "planner": 4, "idea": 5}


@app.get("/api/metrics")
async def get_metrics(days: int = 30):
    """Per-model success + usage metrics for the Metrics tab. Joins volume/cost
    (telemetry:llm) with outcomes (telemetry:outcome) on (role, model), and derives
    the rates that inform model choice: coder success/first-pass/escalation +
    cost-per-success; verdict-role reliability (produced a verdict vs errored)."""
    r = _redis_or_503()
    from reviewer.telemetry import read_all
    from event_bus.outcomes import read_outcomes, read_latency

    vol: dict = {}
    for rec in read_all(r, days=days):
        key = (rec["role"], rec["model"])
        v = vol.setdefault(key, {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0})
        for f in ("cost_usd", "input_tokens", "output_tokens", "calls"):
            v[f] += rec.get(f, 0)
    outs = read_outcomes(r, days=days)
    lat = read_latency(r, days=days)

    def _rate(num, den):
        return round(num / den, 3) if den else None

    models = []
    for (role, model) in set(vol) | set(outs) | set(lat):
        v = vol.get((role, model), {})
        o = outs.get((role, model), {})
        entry = {
            "role": role, "model": model or "(default)",
            "calls": int(v.get("calls", 0)),
            "cost_usd": round(v.get("cost_usd", 0.0), 6),
            "input_tokens": int(v.get("input_tokens", 0)),
            "output_tokens": int(v.get("output_tokens", 0)),
            "avg_latency_ms": lat.get((role, model)),
            "outcomes": o,
        }
        if role == "coder":
            merged, abandoned = o.get("merged", 0), o.get("abandoned", 0)
            total = merged + abandoned
            entry["completed"] = merged
            entry["success_rate"] = _rate(merged, total)
            entry["first_pass_rate"] = _rate(o.get("first_pass", 0), merged)
            entry["escalation_rate"] = _rate(o.get("escalated", 0), total)
            entry["cost_per_success"] = round(v.get("cost_usd", 0.0) / merged, 4) if merged else None
        elif role in ("reviewer", "tester", "security"):
            p, f, e, rl = o.get("pass", 0), o.get("fail", 0), o.get("error", 0), o.get("rate_limited", 0)
            attempts = p + f + e + rl
            entry["verdicts"] = p + f
            entry["reliability"] = _rate(p + f, attempts)
            entry["pass_rate"] = _rate(p, p + f)
            entry["flip_rate"] = _rate(o.get("flip", 0), p + f)          # #4 re-review changed its mind
            entry["rate_limit_rate"] = _rate(rl, attempts)               # #5 provider 429 share
        models.append(entry)

    models.sort(key=lambda m: (_ROLE_ORDER.get(m["role"], 9), -m["calls"]))

    # ── Daily trend + per-model sparklines (single pass over the daily LLM hashes) ──
    import time
    now = time.time()
    dates = [time.strftime("%Y-%m-%d", time.gmtime(now - i * 86_400)) for i in range(days)]
    dates.reverse()  # oldest → newest
    team_day = {d: {"calls": 0, "cost_usd": 0.0} for d in dates}
    model_daily: dict = {}
    for d in dates:
        for field, val in r.hgetall(f"telemetry:llm:{d}").items():
            field = field.decode() if isinstance(field, bytes) else field
            rm, _, metric = field.rpartition(":")
            role, _, model = rm.partition(":")   # model may itself contain ':'
            if not model:
                continue
            if metric == "calls":
                team_day[d]["calls"] += int(val)
                model_daily.setdefault((role, model), {})[d] = \
                    model_daily.get((role, model), {}).get(d, 0) + int(val)
            elif metric == "cost_usd":
                team_day[d]["cost_usd"] += float(val)
    for m in models:
        m["spark"] = [model_daily.get((m["role"], m["model"]), {}).get(d, 0) for d in dates]

    # ── Per-stack success + throughput (from work items, so it includes history) ──
    stories = list_items(item_type="story")
    by_stack: dict = {}
    durs = []
    comp_by_date: dict = {}
    for s in stories:
        state = s.get("state")
        st = by_stack.setdefault(s.get("stack") or "(none)",
                                 {"done": 0, "abandoned": 0, "in_flight": 0, "cyc": []})
        if state == "done":
            st["done"] += 1
            d = _duration_secs(s.get("started_at"), s.get("updated_at"))
            if d is not None:
                st["cyc"].append(d); durs.append(d)
            if s.get("updated_at"):
                day = str(s["updated_at"])[:10]
                comp_by_date[day] = comp_by_date.get(day, 0) + 1
        elif state == "abandoned":
            st["abandoned"] += 1
        elif state in ("in-progress", "in-review", "changes-requested", "ready", "backlog"):
            st["in_flight"] += 1

    stacks = []
    for name, st in by_stack.items():
        total = st["done"] + st["abandoned"]
        stacks.append({
            "stack": name, "done": st["done"], "abandoned": st["abandoned"],
            "in_flight": st["in_flight"],
            "success_rate": round(st["done"] / total, 3) if total else None,
            "avg_cycle_secs": round(sum(st["cyc"]) / len(st["cyc"])) if st["cyc"] else None,
        })
    stacks.sort(key=lambda x: -(x["done"] + x["abandoned"] + x["in_flight"]))

    trend = [{"date": d, "calls": team_day[d]["calls"],
              "cost_usd": round(team_day[d]["cost_usd"], 4),
              "completed": comp_by_date.get(d, 0)} for d in dates]
    avg_cycle = round(sum(durs) / len(durs)) if durs else None

    return {
        "days": days,
        "models": models,
        "coder_compare": [m for m in models if m["role"] == "coder"],
        "avg_cycle_secs": avg_cycle,
        "completed_stories": len(durs),
        "trend": trend,
        "by_stack": stacks,
    }


@app.get("/metrics")
async def get_metrics():
    """
    Prometheus-compatible metrics: concurrency gauges, rate rejections,
    and today's LLM cost/token counters per role and model.
    """
    from fastapi.responses import PlainTextResponse
    text = render_prometheus(_redis_or_503())
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")


# ── Agent log stream (SSE) ────────────────────────────────────────────────────

@app.get("/api/items/{item_id}/log-stream")
async def log_stream(item_id: str):
    """
    Server-Sent Events stream of agent output lines for a work item.

    Streams live while the item is in-progress or changes-requested.
    When the agent finishes, drains remaining lines and sends an `event: done` frame.
    Historical logs (24h TTL) are replayed immediately for completed items.
    """
    from fastapi.responses import StreamingResponse as _SR

    if not get_item(item_id):
        raise HTTPException(status_code=404, detail="item not found")

    r = _redis_conn
    if r is None:
        raise HTTPException(status_code=503, detail="redis not ready")

    key = f"agent_log:{item_id}"
    _ACTIVE = {"in-progress", "changes-requested"}

    async def _generate():
        offset = 0
        elapsed = 0.0
        MAX_S = 1_200.0   # hard stop after 20 min in case state never changes
        POLL_S = 0.5

        while elapsed < MAX_S:
            # Drain any new lines since last poll
            raw_lines = r.lrange(key, offset, -1)
            for raw in raw_lines:
                text = raw.decode("utf-8", errors="replace")
                yield f"data: {text}\n\n"
                offset += 1

            # Check whether the agent is still running
            item = get_item(item_id)
            if not item or item["state"] not in _ACTIVE:
                # Final drain to catch lines written after the last LRANGE
                for raw in r.lrange(key, offset, -1):
                    yield f"data: {raw.decode('utf-8', errors='replace')}\n\n"
                yield "event: done\ndata: finished\n\n"
                return

            await asyncio.sleep(POLL_S)
            elapsed += POLL_S

        yield "event: done\ndata: timeout\n\n"

    return _SR(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    redis_ok = False
    queue_depth: int | None = None
    try:
        if _redis_conn:
            _redis_conn.ping()
            redis_ok = True
            if _queue:
                queue_depth = len(_queue)
    except Exception as exc:
        log.warning("redis_ping_failed", error=str(exc))
    coders_in_flight = 0
    if _redis_conn and redis_ok:
        coders_in_flight = int(_redis_conn.get(_CODER_SLOT_KEY) or 0)
    return {
        "status": "ok",
        "redis": redis_ok,
        "queue_depth": queue_depth,
        "coders_in_flight": coders_in_flight,
        "max_coding_agents": settings.max_coding_agents,
    }
