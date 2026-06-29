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
from event_bus.ci_workflow import CI_WORKFLOW_PATH, CI_WORKFLOW_YAML
from event_bus.config_store import get_config, patch_config
from event_bus.prompt_store import get_prompt, set_prompt, delete_prompt, list_prompts
from event_bus.dispatch import dispatch_forgejo_event
from event_bus.work_store import (
    create_item, get_item, grouped_items, list_items, update_state, set_pr_url, set_repo,
    unlock_next_story, find_item_by_pr_url, get_repo_for_story, STATE_COLORS, get_db
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

    # Auto-advance story when PR is merged in Forgejo
    if event.action == "closed" and event.pull_request.merged:
        pr_url = event.pull_request.html_url
        item = find_item_by_pr_url(pr_url)
        if item:
            updated = update_state(item["id"], "merged")
            unlocked = unlock_next_story(item["id"])
            log.info("pr_merged_auto_advance",
                     item_id=item["id"], pr=event.pr_number,
                     next=unlocked["id"] if unlocked else None)
            if unlocked:
                update_state(unlocked["id"], "in-progress")
                story_prompt = get_prompt(_redis_or_503(), "coder.story")
                asyncio.create_task(_run_coding_agent(
                    unlocked["id"], unlocked["title"], unlocked.get("description") or "", story_prompt,
                ))
                log.info("coding_agent_auto_triggered", story_id=unlocked["id"])
            return {"result": "merged", "item_id": item["id"],
                    "unlocked": unlocked["id"] if unlocked else None}
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
    model_override = (body.get("model_override") or "").strip() or cfg.models.idea

    try:
        from idea_agent.main import expand_idea
        proposal = expand_idea(prompt, model_override=model_override, redis_conn=_redis_conn)
    except ImportError:
        raise HTTPException(status_code=503, detail="idea_agent package not installed")
    except Exception as exc:
        log.error("idea_expansion_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

    item = create_item(
        item_type="idea",
        title=proposal["title"],
        prompt=prompt,
        description=proposal.get("description", ""),
        state="pending-approval",
        model_used=model_override or "",
    )
    log.info("idea_submitted", id=item["id"], title=item["title"])
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
async def approve_item(item_id: str):
    item = get_item(item_id)
    if not item:
        raise HTTPException(status_code=404, detail="item not found")
    if item["state"] != "pending-approval":
        raise HTTPException(status_code=409, detail=f"item is '{item['state']}', not pending-approval")
    updated = update_state(item_id, "approved")
    log.info("idea_approved", id=item_id, title=item["title"])
    # Kick off planner in the background so the response returns immediately
    asyncio.create_task(_run_planner(item_id, item["title"], item["description"] or ""))
    return updated


def _slugify(text: str) -> str:
    """Convert a title to a valid Forgejo repo name (lowercase, hyphens, max 40 chars)."""
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40]


def _provision_project_repo(idea_id: str, title: str) -> str:
    """
    Create a Forgejo repo for this idea (if it doesn't exist) and register
    the event-bus webhook on it. Returns 'owner/repo' or '' on failure.
    """
    try:
        from coding_agent.forgejo_client import ForgejoClient
        from coding_agent.config import settings as coding_settings
    except ImportError:
        log.warning("coding_agent_not_installed_skipping_repo_provision")
        return ""

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
            # Commit the default CI workflow on fresh repos so the Actions runner
            # tests every push/PR and reports a status the Tester verdict can gate on.
            if created:
                try:
                    fj.create_file(
                        owner, repo_name, CI_WORKFLOW_PATH, CI_WORKFLOW_YAML,
                        message="ci: add default test workflow", branch="main",
                    )
                except Exception as exc:
                    log.warning("ci_workflow_commit_failed", repo=repo_full, error=str(exc))
                # Protect main: block direct pushes (agents work via PRs). No required
                # approvals/status — the reviewer auto-merges and a hard gate would
                # deadlock that; the event-bus verdicts remain the real gate.
                try:
                    fj.set_branch_protection(owner, repo_name, "main")
                except Exception as exc:
                    log.warning("branch_protection_failed", repo=repo_full, error=str(exc))
                # Grant the reviewer-bot write access so the reviewer agent can
                # comment/merge with its own least-privilege token (not the admin token).
                reviewer_user = settings.forgejo_reviewer_user
                if reviewer_user and reviewer_user != owner:
                    try:
                        fj.add_collaborator(owner, repo_name, reviewer_user, "write")
                    except Exception as exc:
                        log.warning("reviewer_collaborator_failed", repo=repo_full, error=str(exc))
            # Register webhook (idempotent — Forgejo allows duplicates but we accept that)
            webhook_url = f"http://event-bus:8080/webhook/forgejo"
            fj.create_webhook(owner, repo_name, webhook_url, settings.forgejo_webhook_secret)
        log.info("project_repo_provisioned", repo=repo_full, idea=idea_id, ci_workflow=created)
    except Exception as exc:
        log.error("project_repo_provision_failed", repo=repo_full, error=str(exc))
        return ""

    return repo_full


async def _run_planner(item_id: str, title: str, description: str) -> None:
    """Run the Planner Agent in a thread and save resulting stories to SQLite."""
    cfg = get_config(_redis_or_503())
    model = cfg.models.planner

    # Create a dedicated Forgejo repo for this project before decomposing
    repo_full = await asyncio.to_thread(_provision_project_repo, item_id, title)
    if repo_full:
        set_repo(item_id, repo_full)
        log.info("idea_repo_set", idea=item_id, repo=repo_full)

    try:
        from planner_agent.main import run_planner
        plan = await asyncio.to_thread(run_planner, item_id, title, description, model,
                                       repo_full_name=repo_full, redis_conn=_redis_conn)
    except ImportError:
        log.error("planner_agent_not_installed")
        return
    except Exception as exc:
        log.error("planner_failed", id=item_id, error=str(exc))
        return

    stories = plan.get("stories", [])
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


def _run_coding_agent_sandboxed_sync(
    item_id: str,
    title: str,
    description: str,
    story_prompt: str,
    log_cb,
) -> dict:
    """Blocking: spawn an ephemeral Docker container for one coding agent run.
    Returns the result dict from sandbox_runner (same shape as run_coding_agent).
    """
    import docker as _docker_sdk

    client = _docker_sdk.from_env()

    pass_through = [
        "FORGEJO_API_TOKEN", "FORGEJO_BASE_URL", "FORGEJO_GIT_URL",
        "OPENROUTER_API_KEY", "MODEL_CODER", "DEFAULT_REPO", "ANTHROPIC_API_KEY",
    ]
    env = {k: _os.environ[k] for k in pass_through if k in _os.environ}
    env.update({
        "STORY_ID": item_id,
        "STORY_TITLE": title[:500],
        "STORY_DESCRIPTION": (description or "")[:4000],
        "STORY_PROMPT": (story_prompt or "")[:2000],
    })

    volumes: dict = {}
    if settings.sandbox_opencode_bin:
        volumes[settings.sandbox_opencode_bin] = {"bind": "/usr/local/bin/opencode", "mode": "ro"}
    if settings.sandbox_opencode_config:
        volumes[settings.sandbox_opencode_config] = {"bind": "/root/.config/opencode", "mode": "ro"}

    container = client.containers.create(
        image=settings.sandbox_image,
        command=["python", "-m", "coding_agent.sandbox_runner"],
        environment=env,
        volumes=volumes or None,
        network=settings.sandbox_network,
        mem_limit=settings.sandbox_memory,
        nano_cpus=int(float(settings.sandbox_cpus) * 1_000_000_000),
        labels={"dev-agents.story-id": item_id, "dev-agents.sandbox": "true"},
    )

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
    # Enforce concurrency cap — leave story queued in ready if full
    if not _coder_slot_acquire():
        log.info("coder_cap_hit", id=item_id, cap=settings.max_coding_agents)
        update_state(item_id, "ready")
        return

    # Prefer the explicit repo field (set by planner) over the description directive
    repo = get_repo_for_story(item_id, default=settings.default_repo)
    if repo and f"repo: {repo}" not in (description or ""):
        description = f"repo: {repo}\n{description}"
    log_cb = _make_log_cb(item_id)
    log.info("coding_agent_dispatch", id=item_id, mode=settings.sandbox_mode)
    try:
        if settings.sandbox_mode == "docker":
            result = await asyncio.to_thread(
                _run_coding_agent_sandboxed_sync,
                item_id, title, description, story_prompt, log_cb,
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
                                             story_prompt=story_prompt, log_line=log_cb)
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
        update_state(item_id, "in-review")
        return
    finally:
        _expire_log(item_id)

    if result.get("status") == "success":
        update_state(item_id, "in-review")
        log.info("recode_agent_complete", item_id=item_id, sha=result.get("sha", "")[:8])
    else:
        log.warning("recode_no_changes", item_id=item_id)
        update_state(item_id, "in-review")


_MAX_RECODE_RETRIES = 3


@app.post("/internal/pr-merged", status_code=status.HTTP_200_OK)
async def internal_pr_merged(payload: dict):
    """Called by the reviewer gate after auto-merging a PR via the API."""
    pr_url = payload.get("pr_url", "")
    pr_number = payload.get("pr_number", 0)
    item = find_item_by_pr_url(pr_url) if pr_url else None
    if not item:
        raise HTTPException(status_code=404, detail="no story found for this PR")
    updated = update_state(item["id"], "merged")
    unlocked = unlock_next_story(item["id"])
    log.info("internal_pr_merged", item_id=item["id"], pr=pr_number,
             next=unlocked["id"] if unlocked else None)
    if unlocked:
        update_state(unlocked["id"], "in-progress")
        story_prompt = get_prompt(_redis_or_503(), "coder.story")
        asyncio.create_task(_run_coding_agent(
            unlocked["id"], unlocked["title"], unlocked.get("description") or "", story_prompt,
        ))
        log.info("coding_agent_auto_triggered", story_id=unlocked["id"])
    return {"merged": updated, "unlocked": unlocked["id"] if unlocked else None}


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
        try:
            await asyncio.to_thread(
                fix_pr_review,
                item["id"], item["title"], item.get("description", ""),
                payload.get("head_ref", ""), repo_full_name,
                review_comments,
                review_fix_prompt=review_fix_prompt,
                log_line=log_cb,
            )
        finally:
            _expire_log(item["id"])

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
    updated = update_state(item_id, "merged")
    unlocked = unlock_next_story(item_id)
    if unlocked:
        log.info("story_unlocked", id=unlocked["id"], seq=unlocked.get("sequence"), title=unlocked["title"])
    log.info("story_merged", id=item_id, next_unlocked=unlocked["id"] if unlocked else None)
    return {"merged": updated, "unlocked": unlocked}


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
