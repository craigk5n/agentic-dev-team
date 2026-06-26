"""
Event bus — webhook receiver.

POST /webhook/plane   — receives Plane CE webhooks
POST /webhook/forgejo — receives Forgejo webhooks
GET  /health          — liveness probe
"""

from __future__ import annotations
import structlog
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import redis
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from rq import Queue

import json as _json
from dataclasses import asdict

from event_bus.config import settings
from event_bus.config_store import get_config, patch_config
from event_bus.dispatch import dispatch_forgejo_event, dispatch_plane_event
from event_bus.telemetry import get_telemetry_summary, render_prometheus
from event_bus.events.forgejo import ForgejoPREvent
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

    # Always validate — verify_forgejo returns False when secret is empty (fail-closed)
    if not verify_forgejo(body, x_gitea_signature or "", settings.forgejo_webhook_secret):
        log.warning("forgejo_signature_invalid")
        raise HTTPException(status_code=403, detail="invalid signature")

    if x_gitea_event != "pull_request":
        return {"result": "skipped", "reason": f"unhandled event type: {x_gitea_event}"}

    try:
        payload = _json.loads(body)
        event = ForgejoPREvent.model_validate(payload)
    except _json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    except ValidationError as exc:
        log.warning("forgejo_parse_error", error=str(exc))
        raise HTTPException(status_code=400, detail="payload validation failed") from exc

    outcome = dispatch_forgejo_event(event, _queue_or_503())
    log.info("forgejo_webhook", result=outcome.result, reason=outcome.reason, job=outcome.job_id)
    return {"result": outcome.result, "job_id": outcome.job_id}


# ── Ideas API (Phase 5) ────────────────────────────────────────────────────────

@app.post("/api/ideas", status_code=status.HTTP_202_ACCEPTED)
async def submit_idea(request: Request):
    """
    Submit a new feature idea. The Idea Agent expands the prompt and creates
    a Plane issue with state 'Pending Approval' for human review.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")

    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="'prompt' field required")

    cfg = get_config(_redis_or_503())

    # Priority: request body → runtime config (Config tab) → env var
    project_id = body.get("project_id") or cfg.project.plane_project_id or settings.plane_project_id
    if not project_id:
        raise HTTPException(
            status_code=422,
            detail="project_id required — set it in Config → Project, PLANE_PROJECT_ID env var, or pass in the form",
        )

    # Model override: request body → runtime config → env var default
    model_override = (body.get("model_override") or "").strip()
    if not model_override:
        model_override = cfg.models.idea

    try:
        from idea_agent.main import submit_idea as _submit
        result = _submit(
            prompt,
            project_id=project_id,
            workspace_slug=settings.plane_workspace_slug,
            model_override=model_override,
        )
    except ImportError:
        raise HTTPException(status_code=503, detail="idea_agent package not installed")
    except Exception as exc:
        log.error("idea_submission_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

    log.info("idea_submitted", issue_id=result.get("issue_id"), title=result.get("title"))
    return result


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
