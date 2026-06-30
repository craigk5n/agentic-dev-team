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
from event_bus.ci_workflow import CI_WORKFLOW_PATH
from event_bus.config_store import get_config, patch_config
from event_bus.prompt_store import get_prompt, set_prompt, delete_prompt, list_prompts
from event_bus.dispatch import dispatch_forgejo_event
from event_bus.work_store import (
    create_item, get_item, grouped_items, list_items, update_state, set_pr_url, set_repo,
    unlock_next_story, find_item_by_pr_url, get_repo_for_story, set_stack_sdlc,
    get_stack_sdlc_for_story, set_style_guides, get_style_guides_for_story,
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


def _coder_slot_release_and_dispatch() -> None:
    """Release one coding slot, then trigger the next queued ready story if a slot is free."""
    if _redis_conn:
        _redis_conn.decr(_CODER_SLOT_KEY)
        _redis_conn.expire(_CODER_SLOT_KEY, 86_400)
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
    get_db()  # initialise SQLite schema
    log.info("event_bus_started", redis=settings.redis_url)
    yield
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

    # A new commit on the PR resets the recode retry counter
    if event.action in ("synchronize", "synchronized"):
        retry_key = f"recode_retries:{event.repo_full_name}:{event.pr_number}"
        _redis_or_503().delete(retry_key)

    outcome = dispatch_forgejo_event(event, _queue_or_503())
    log.info("forgejo_webhook", result=outcome.result, reason=outcome.reason, job=outcome.job_id)
    return {"result": outcome.result, "job_id": outcome.job_id}


# ── Ideas API ─────────────────────────────────────────────────────────────────

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
    )
    log.info("idea_submitted", id=item["id"], title=item["title"], stack=stack, sdlc=sdlc,
             guides=guides)
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


@app.get("/api/items")
async def list_work_items(response: Response):
    """Return all work items grouped by state with state metadata."""
    response.headers["Cache-Control"] = "no-store"
    groups = grouped_items()
    # Enrich items that have a PR URL with live verdict data from Redis
    for items in groups.values():
        for item in items:
            if item.get("pr_url"):
                item["verdicts"] = _fetch_verdicts(item["pr_url"])
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
    model = cfg.models.planner

    # Resolve the stack chosen at approval (falls back to generic/standard).
    item = get_item(item_id) or {}
    cat = get_catalog()
    stack = cat.get_stack(item.get("stack"))
    sdlc = cat.get_sdlc(item.get("sdlc"))

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
            redis_conn=_redis_conn,
        )
    except ImportError:
        log.error("planner_agent_not_installed")
        return
    except Exception as exc:
        log.error("planner_failed", id=item_id, error=str(exc))
        return

    stories = plan.get("stories", [])
    idea_guides = [g for g in (item.get("style_guides") or "").split(",") if g]
    first_story: dict = {}
    for i, story in enumerate(stories):
        # Only the first story starts ready; the rest are backlog until their predecessor merges
        state = "ready" if i == 0 else "backlog"
        story_item = create_item(
            item_type="story",
            title=story["title"],
            description=story.get("description", ""),
            state=state,
            parent_id=item_id,
            sequence=i + 1,
            model_used=model,
            repo=repo_full,
            stack=stack.id,
            sdlc=sdlc.id,
            style_guides=idea_guides,
        )
        log.info("story_created", id=story_item["id"], seq=i + 1, state=state,
                 title=story_item["title"], repo=repo_full)
        if i == 0:
            first_story = story_item

    log.info(
        "planner_complete",
        idea_id=item_id,
        module=plan.get("module_name"),
        story_count=len(plan.get("stories", [])),
        repo=repo_full,
    )

    if stories:
        update_state(first_story["id"], "in-progress")
        story_prompt = get_prompt(_redis_conn, "coder.story")
        asyncio.create_task(_run_coding_agent(
            first_story["id"], first_story["title"], first_story["description"] or "", story_prompt,
        ))
        log.info("coding_agent_auto_triggered", story_id=first_story["id"])


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


def _run_coding_agent_sandboxed_sync(
    item_id: str,
    title: str,
    description: str,
    story_prompt: str,
    log_cb,
    coder_image: str = "",
    test_command: str = "",
    install_command: str = "",
) -> dict:
    """Blocking: spawn an ephemeral Docker container for one coding agent run.
    Returns the result dict from sandbox_runner (same shape as run_coding_agent).
    """
    import docker as _docker_sdk

    client = _docker_sdk.from_env()

    env = {k: _os.environ[k] for k in _CODER_SANDBOX_ENV if k in _os.environ}
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

        if result_json is not None:
            return result_json
        if exit_code != 0:
            return {"status": "error", "item_id": item_id,
                    "error": f"sandbox exited with code {exit_code}"}
        return {"status": "no_changes", "item_id": item_id, "error": "no result sentinel"}
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


async def _run_coding_agent(item_id: str, title: str, description: str, story_prompt: str = "") -> None:
    """Run the Coding Agent in a thread; update state and PR URL on completion."""
    # Cost backstop — leave the story in ready so it resumes when budget frees up
    cfg = get_config(_redis_or_503())
    if over_budget(_redis_conn, cfg.limits.max_cost_usd_daily):
        log.warning("coder_skipped_cost_cap", id=item_id)
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
    try:
        if settings.sandbox_mode == "docker":
            result = await asyncio.to_thread(
                _run_coding_agent_sandboxed_sync,
                item_id, title, description, story_prompt, log_cb, stack.coder_image,
                stack.test_command, stack.install_command,
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
        update_state(item_id, "ready")  # unclaim so it can be retried
        _coder_slot_release_and_dispatch()
        return
    finally:
        _expire_log(item_id)

    if result.get("status") == "success":
        update_state(item_id, "in-review")
        if result.get("pr_url"):
            set_pr_url(item_id, result["pr_url"])
        log.info("coding_agent_complete", id=item_id, pr=result.get("pr_url"))
    elif result.get("status") == "error":
        # Agent crashed (OOM, network failure, etc.) — reset so it can be retried
        log.error("coding_agent_error", id=item_id, error=result.get("error", "unknown"))
        update_state(item_id, "ready")
    else:
        # Agent ran but found nothing to implement — mark done and advance sequence
        log.warning("coding_agent_no_changes", id=item_id)
        update_state(item_id, "done")
        unlock_next_story(item_id)  # backlog → ready; release+dispatch below will trigger it

    # Record coder call — opencode runs as subprocess so no token counts available
    if _redis_conn:
        try:
            from coding_agent.config import settings as _cs
            model = _cs.model_coder or "opencode"
            import time
            key = f"telemetry:llm:{time.strftime('%Y-%m-%d')}"
            prefix = f"coder:{model}"
            _redis_conn.hincrby(key, f"{prefix}:calls", 1)
            _redis_conn.expire(key, 30 * 86_400)
        except Exception:
            pass

    # Release slot and trigger next queued story (after all state updates are done)
    _coder_slot_release_and_dispatch()


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
    else:
        update_state(item_id, "changes-requested")
        log.warning("post_merge_ci_failed", item_id=item_id, ci=result, sha=sha[:8])
        _post_merge_fix(item_id, result)


def _on_story_merged(item_id: str, repo_full_name: str = "") -> dict | None:
    """A PR merged → 'merged' is transient; post-merge CI on main decides done vs fix.
    The next story is NOT unlocked until this one reaches 'done' (CI-verified main)."""
    updated = update_state(item_id, "merged")
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
    Capped at _MAX_RECODE_RETRIES attempts per PR to break infinite loops.
    """
    repo_full_name = payload.get("repo_full_name", "")
    pr_number = payload.get("pr_number", 0)
    pr_url = payload.get("pr_url", "")
    feedback = payload.get("feedback", "Automated checks failed — see PR comments for details.")

    item = find_item_by_pr_url(pr_url) if pr_url else None
    if not item:
        raise HTTPException(status_code=404, detail="no story found for this PR")

    # Retry cap — prevent infinite recode loops
    r = _redis_or_503()
    retry_key = f"recode_retries:{repo_full_name}:{pr_number}"
    retries = int(r.get(retry_key) or 0)
    if retries >= _MAX_RECODE_RETRIES:
        log.warning("recode_retry_cap_reached", pr=pr_number, repo=repo_full_name, retries=retries)
        _post_pr_comment(
            repo_full_name, pr_number,
            f"🤖 **Auto-fix gave up after {retries} attempts.** The checks are still failing — "
            "this PR needs human attention.",
        )
        update_state(item["id"], "changes-requested")
        return {"status": "retry_cap_reached", "retries": retries, "item_id": item["id"]}
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


# ── Telemetry (Phase 7) ────────────────────────────────────────────────────────

@app.get("/api/telemetry")
async def get_telemetry(days: int = 7):
    """
    Return LLM cost/token telemetry, current concurrency, and rate-limit rejection
    counts aggregated over the last `days` days (default: 7).
    """
    return get_telemetry_summary(_redis_or_503(), days=days)


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
