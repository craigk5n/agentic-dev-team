"""
Event bus — webhook receiver + work item store.

POST /webhook/plane   — receives Plane CE webhooks (legacy)
POST /webhook/forgejo — receives Forgejo webhooks
GET  /health          — liveness probe

Work items (replaces Plane):
POST /api/ideas                    — submit idea, LLM expands, saves to SQLite
GET  /api/items                    — list all items grouped by state
GET  /api/items/{id}               — get single item with full description
POST /api/items/{id}/approve       — approve pending idea → triggers Planner Agent
POST /api/items/{id}/reject        — reject pending idea
POST /api/items/migrate-from-plane — import existing Plane issues
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
from dataclasses import asdict

from event_bus.config import settings
from event_bus.config_store import get_config, patch_config
from event_bus.prompt_store import get_prompt, set_prompt, delete_prompt, list_prompts
from event_bus.dispatch import dispatch_forgejo_event, dispatch_plane_event
from event_bus.work_store import (
    create_item, get_item, grouped_items, update_state, set_pr_url,
    unlock_next_story, find_item_by_pr_url, STATE_COLORS, get_db
)
from event_bus.telemetry import get_telemetry_summary, render_prometheus
from event_bus.events.forgejo import ForgejoPREvent, ForgejoReviewEvent
from event_bus.events.plane import PlaneEvent
from event_bus.signatures import verify_forgejo, verify_plane

structlog.configure(
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, settings.log_level.upper(), logging.INFO)
    )
)
log = structlog.get_logger()

_redis_conn: redis.Redis | None = None
_queue: Queue | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis_conn, _queue
    # Warn loudly if secrets are absent — fail-closed means webhooks will be
    # rejected with 403 until secrets are configured.
    if not settings.plane_webhook_secret:
        log.warning("plane_webhook_secret_not_set — all Plane webhooks will be rejected")
    if not settings.forgejo_webhook_secret:
        log.warning("forgejo_webhook_secret_not_set — all Forgejo webhooks will be rejected")
    _redis_conn = redis.from_url(settings.redis_url, decode_responses=False)
    _queue = Queue("agent-jobs", connection=_redis_conn)
    get_db()  # initialise SQLite schema
    log.info("event_bus_started", redis=settings.redis_url)
    yield
    if _redis_conn:
        _redis_conn.close()


app = FastAPI(title="dev-agents event bus", lifespan=lifespan)

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


# ── Plane webhook ──────────────────────────────────────────────────────────────

@app.post("/webhook/plane", status_code=status.HTTP_202_ACCEPTED)
async def plane_webhook(
    request: Request,
    x_plane_signature: str | None = Header(default=None),
):
    body = await request.body()

    # Always validate — verify_plane returns False when secret is empty (fail-closed)
    if not verify_plane(body, x_plane_signature or "", settings.plane_webhook_secret):
        log.warning("plane_signature_invalid")
        raise HTTPException(status_code=403, detail="invalid signature")

    try:
        payload = _json.loads(body)
        event = PlaneEvent.model_validate(payload)
    except _json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    except ValidationError as exc:
        log.warning("plane_parse_error", error=str(exc))
        raise HTTPException(status_code=400, detail="payload validation failed") from exc

    outcome = dispatch_plane_event(event, _queue_or_503(), settings.plane_workspace_slug)
    log.info("plane_webhook", result=outcome.result, reason=outcome.reason, job=outcome.job_id)
    return {"result": outcome.result, "job_id": outcome.job_id}


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
                 event=x_gitea_event,
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
            return {"result": "merged", "item_id": item["id"],
                    "unlocked": unlocked["id"] if unlocked else None}
        log.info("pr_merged_no_story", pr_url=pr_url)
        return {"result": "skipped", "reason": "no matching story for PR"}

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
        proposal = expand_idea(prompt, model_override=model_override)
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
        model_used=model_override or settings.model_idea,
    )
    log.info("idea_submitted", id=item["id"], title=item["title"])
    return item


# ── Work items API ────────────────────────────────────────────────────────────

@app.get("/api/items")
async def list_work_items(response: Response):
    """Return all work items grouped by state with state metadata."""
    response.headers["Cache-Control"] = "no-store"
    groups = grouped_items()
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


async def _run_planner(item_id: str, title: str, description: str) -> None:
    """Run the Planner Agent in a thread and save resulting stories to SQLite."""
    cfg = get_config(_redis_or_503())
    model = cfg.models.planner
    try:
        from planner_agent.main import run_planner
        plan = await asyncio.to_thread(run_planner, item_id, title, description, model)
    except ImportError:
        log.error("planner_agent_not_installed")
        return
    except Exception as exc:
        log.error("planner_failed", id=item_id, error=str(exc))
        return

    stories = plan.get("stories", [])
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
        )
        log.info("story_created", id=story_item["id"], seq=i + 1, state=state, title=story_item["title"])

    log.info(
        "planner_complete",
        idea_id=item_id,
        module=plan.get("module_name"),
        story_count=len(plan.get("stories", [])),
    )


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


async def _run_coding_agent(item_id: str, title: str, description: str, story_prompt: str = "") -> None:
    """Run the Coding Agent in a thread; update state and PR URL on completion."""
    try:
        from coding_agent.main import run_coding_agent
        result = await asyncio.to_thread(run_coding_agent, item_id, title, description,
                                         story_prompt=story_prompt)
    except ImportError:
        log.error("coding_agent_not_installed")
        update_state(item_id, "ready")  # unclaim
        return
    except Exception as exc:
        log.error("coding_agent_failed", id=item_id, error=str(exc))
        update_state(item_id, "ready")  # unclaim so it can be retried
        return

    if result.get("status") == "success":
        update_state(item_id, "in-review")
        if result.get("pr_url"):
            set_pr_url(item_id, result["pr_url"])
        log.info("coding_agent_complete", id=item_id, pr=result.get("pr_url"))
    else:
        log.warning("coding_agent_no_changes", id=item_id)
        update_state(item_id, "ready")


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
    try:
        from coding_agent.main import fix_pr_review
        result = await asyncio.to_thread(
            fix_pr_review,
            item["id"], item["title"], item.get("description", ""),
            review_event.head_ref, review_event.repo_full_name,
            review_comments,
            review_fix_prompt=review_fix_prompt,
        )
    except Exception as exc:
        log.error("recode_agent_failed", item_id=item_id, error=str(exc))
        update_state(item_id, "in-review")
        return

    if result.get("status") == "success":
        update_state(item_id, "in-review")
        log.info("recode_agent_complete", item_id=item_id, sha=result.get("sha", "")[:8])
    else:
        log.warning("recode_no_changes", item_id=item_id)
        update_state(item_id, "in-review")


@app.post("/internal/recode-for-pr", status_code=status.HTTP_202_ACCEPTED)
async def internal_recode_for_pr(payload: dict):
    """
    Called by the reviewer gate (worker) when checks fail.
    Triggers the recode agent without going through the Forgejo webhook chain.
    """
    repo_full_name = payload.get("repo_full_name", "")
    pr_number = payload.get("pr_number", 0)
    pr_url = payload.get("pr_url", "")
    feedback = payload.get("feedback", "Automated checks failed — see PR comments for details.")

    item = find_item_by_pr_url(pr_url) if pr_url else None
    if not item:
        raise HTTPException(status_code=404, detail="no story found for this PR")

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

    log.info("internal_recode_triggered", item_id=item["id"], pr=pr_number, repo=repo_full_name)
    review_fix_prompt = get_prompt(_redis_or_503(), "coder.review_fix")
    from coding_agent.main import fix_pr_review
    asyncio.create_task(asyncio.to_thread(
        fix_pr_review,
        item["id"], item["title"], item.get("description", ""),
        payload.get("head_ref", ""), repo_full_name,
        review_comments,
        review_fix_prompt=review_fix_prompt,
    ))
    update_state(item["id"], "changes-requested")
    return {"status": "recode_started", "item_id": item["id"]}


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


@app.post("/api/items/migrate-from-plane", status_code=status.HTTP_200_OK)
async def migrate_from_plane():
    """
    Pull existing issues from Plane and import them into the SQLite work store.
    Safe to run multiple times — skips items whose ID already exists.
    """
    import httpx
    from event_bus.work_store import get_db as _get_db
    plane_state_map = {
        "78caf460-11bd-4e63-8e4d-9a3e1dc28d95": "pending-approval",
        "afa9c777-c63e-44f8-b526-a5fc9330e910": "approved",
        "88435ad2-c302-4270-b60e-32c4f2e52858": "rejected",
        "87f5d6b7-6352-4ec4-b4f8-1d047b697fe6": "ready",
        "a65f2df2-7cc6-4846-9bd2-8a9d6b718765": "in-progress",
        "9d27a8e7-7d58-4244-9b23-0b8ee9d9b850": "in-review",
        "9e7d182c-59dd-48c3-9b95-c635ba1fbec3": "changes-requested",
        "bbc570d1-a7c9-406b-8102-a3f38e6684d5": "merged",
        "889d04d3-4db8-41db-8072-6a5c793a96d9": "done",
    }
    cfg = get_config(_redis_or_503())
    project_id = cfg.project.plane_project_id or settings.plane_project_id
    if not project_id:
        raise HTTPException(status_code=422, detail="No project_id configured")

    url = f"{settings.plane_base_url}/api/v1/workspaces/{settings.plane_workspace_slug}/projects/{project_id}/issues/?per_page=100"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers={"X-API-Key": settings.plane_api_token})
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Plane returned {resp.status_code}")

    imported, skipped = 0, 0
    for issue in resp.json().get("results", []):
        existing = get_item(issue["id"])
        if existing:
            skipped += 1
            continue
        state = plane_state_map.get(issue.get("state", ""), "pending-approval")
        import re
        description = re.sub(r"<[^>]+>", "", issue.get("description_html") or "").strip()
        create_item(
            item_type="idea",
            title=issue["name"],
            description=description,
            state=state,
            item_id=issue["id"],
            created_at=issue.get("created_at"),
        )
        imported += 1

    log.info("plane_migration_done", imported=imported, skipped=skipped)
    return {"imported": imported, "skipped": skipped}


# ── Board / issue browser (Plane) — removed; replaced by /api/items ───────────


# ── Runtime config API (Phase 6) ──────────────────────────────────────────────

@app.get("/api/config")
async def get_runtime_config():
    """
    Return the current runtime configuration: gate flags and per-role model overrides.

    Gates:
      idea_approval    — always true (read-only); ideas require human approval in Plane
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
    return {"status": "ok", "redis": redis_ok, "queue_depth": queue_depth}
