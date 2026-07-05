"""
Telemetry aggregation and Prometheus metrics rendering for the event bus.

Reads from:
  telemetry:llm:{date}      — cost/token data written by reviewer.telemetry
  limits:concurrency:{role} — current in-flight job count
  limits:rejected:{role}    — cumulative rate/concurrency rejections

Exposed at:
  GET /api/telemetry  — JSON summary
  GET /metrics        — Prometheus text format
"""

from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis

_ALL_ROLES = ("coder", "idea", "planner", "reviewer", "tester", "security")


def get_telemetry_summary(r: "redis.Redis", days: int = 7) -> dict:
    """
    Return aggregated telemetry: costs, tokens, rejections, and current concurrency.
    """
    try:
        from reviewer.telemetry import (read_all, read_stack_usage, read_project_usage,
                                         read_story_usage, read_run_usage)
        llm_records = read_all(r, days=days)
        stack_records = read_stack_usage(r, days=days)
        project_records = read_project_usage(r, days=days)
        story_records = read_story_usage(r, days=days)
        run_records = read_run_usage(r, days=days)
    except ImportError:
        llm_records = []
        stack_records = []
        project_records = []
        story_records = []
        run_records = []

    from event_bus.limits import get_concurrency, get_rejected, get_rate_window_count

    # Aggregate LLM costs by role
    by_role: dict[str, dict] = {}
    for rec in llm_records:
        role = rec.get("role", "unknown")
        if role not in by_role:
            by_role[role] = {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "calls": 0}
        by_role[role]["cost_usd"] += rec.get("cost_usd", 0.0)
        by_role[role]["input_tokens"] += rec.get("input_tokens", 0)
        by_role[role]["output_tokens"] += rec.get("output_tokens", 0)
        by_role[role]["calls"] += rec.get("calls", 0)

    concurrency = {role: get_concurrency(r, role) for role in _ALL_ROLES}
    rejected = {role: get_rejected(r, role) for role in _ALL_ROLES}
    rate_current = {role: get_rate_window_count(r, role) for role in _ALL_ROLES}

    total_cost = sum(v["cost_usd"] for v in by_role.values())

    # Story 3.3: coder-cost provenance per story (measured vs estimated), read from the
    # flag _record_coder_usage writes. Lets analyses filter out estimated spend.
    def _coder_cost_source(story_id: str) -> str:
        try:
            v = r.get(f"coder_cost_src:{story_id}")
            if isinstance(v, (bytes, bytearray)):
                v = v.decode()
            return v or "unknown"
        except Exception:
            return "unknown"

    # Reconciliation report: total spend vs spend attributed to a story. A large
    # unattributed share flags an attribution gap (e.g. the coder black-box undercount).
    story_total = sum(rec.get("cost_usd", 0.0) for rec in story_records)
    reconciliation = {
        "total_usd": round(total_cost, 6),
        "story_attributed_usd": round(story_total, 6),
        "unattributed_usd": round(total_cost - story_total, 6),
        "unattributed_pct": round((total_cost - story_total) / total_cost * 100, 2)
        if total_cost else 0.0,
    }

    # Daily spend vs. the configured cap (the cost backstop)
    from event_bus.config_store import get_config
    from event_bus.cost_guard import today_spend
    cap = get_config(r).limits.max_cost_usd_daily
    spend = today_spend(r)

    return {
        "period_days": days,
        "total_cost_usd": round(total_cost, 6),
        "daily_spend_usd": spend,
        "max_cost_usd_daily": cap,
        "cost_capped": bool(cap > 0 and spend >= cap),
        "by_role": {
            role: {
                **by_role.get(role, {"cost_usd": 0.0, "input_tokens": 0,
                                     "output_tokens": 0, "calls": 0}),
                "current_concurrent": concurrency.get(role, 0),
                "rate_rejected_total": rejected.get(role, 0),
                "rate_current_minute": rate_current.get(role, 0),
            }
            for role in _ALL_ROLES
        },
        "by_stack": {
            rec["stack"]: {
                "cost_usd": round(rec["cost_usd"], 6),
                "input_tokens": rec["input_tokens"],
                "output_tokens": rec["output_tokens"],
                "calls": rec["calls"],
            }
            for rec in stack_records
        },
        # Per-project spend. Keyed by project id; the API layer resolves the display
        # title (kept out of here to avoid a work_store dependency in telemetry).
        "by_project": [
            {
                "project": rec["project"],
                "cost_usd": round(rec["cost_usd"], 6),
                "input_tokens": rec["input_tokens"],
                "output_tokens": rec["output_tokens"],
                "calls": rec["calls"],
            }
            for rec in project_records
        ],
        # HS-9: per-story spend (coder + verdict), keyed by story item id — answers
        # "which stories cost the most". The API layer resolves the title.
        "by_story": [
            {
                "story": rec["story"],
                "cost_usd": round(rec["cost_usd"], 6),
                "input_tokens": rec["input_tokens"],
                "output_tokens": rec["output_tokens"],
                "calls": rec["calls"],
                "coder_cost_source": _coder_cost_source(rec["story"]),
            }
            for rec in story_records
        ],
        # Story 3.3: attribution reconciliation (total vs per-story-attributed spend).
        "reconciliation": reconciliation,
        # Story 5.1: per-run spend for experiment arms, keyed by run id.
        "by_run": [
            {
                "run": rec["run"],
                "cost_usd": round(rec["cost_usd"], 6),
                "input_tokens": rec["input_tokens"],
                "output_tokens": rec["output_tokens"],
                "calls": rec["calls"],
            }
            for rec in run_records
        ],
        "daily": llm_records,
    }


def render_prometheus(r: "redis.Redis") -> str:
    """
    Render current metrics in Prometheus text exposition format.

    Story 3.3 decision: per-story cost is deliberately NOT exported here. Story ids are
    unbounded and high-cardinality — a label per story would blow up the Prometheus
    time-series count. Per-story spend (with its measured/estimated provenance) is served
    JSON-only via GET /api/telemetry `by_story`, which is the right surface for it.
    """
    from event_bus.limits import get_concurrency, get_rejected, get_rate_window_count

    lines: list[str] = []

    def _metric(name: str, help_text: str, metric_type: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {metric_type}")

    _metric("agent_concurrent_jobs", "Current number of in-flight jobs per role", "gauge")
    for role in _ALL_ROLES:
        lines.append(f'agent_concurrent_jobs{{role="{role}"}} {get_concurrency(r, role)}')

    _metric("agent_rate_rejected_total", "Total jobs rejected by rate or concurrency limits", "counter")
    for role in _ALL_ROLES:
        lines.append(f'agent_rate_rejected_total{{role="{role}"}} {get_rejected(r, role)}')

    _metric("agent_rate_current_minute", "Jobs submitted in the current 1-minute window", "gauge")
    for role in _ALL_ROLES:
        lines.append(f'agent_rate_current_minute{{role="{role}"}} {get_rate_window_count(r, role)}')

    # LLM cost and token metrics from reviewer telemetry
    try:
        from reviewer.telemetry import read_all
        records = read_all(r, days=1)  # today only for Prometheus gauges

        by_role_model: dict[tuple, dict] = {}
        for rec in records:
            key = (rec["role"], rec["model"])
            if key not in by_role_model:
                by_role_model[key] = {"cost_usd": 0.0, "input_tokens": 0,
                                       "output_tokens": 0, "calls": 0}
            for field in ("cost_usd", "input_tokens", "output_tokens", "calls"):
                by_role_model[key][field] += rec.get(field, 0)

        if by_role_model:
            _metric("agent_llm_cost_usd_today",
                    "LLM cost in USD for today per role and model", "gauge")
            for (role, model), vals in by_role_model.items():
                lines.append(
                    f'agent_llm_cost_usd_today{{role="{role}",model="{model}"}} '
                    f'{round(vals["cost_usd"], 8)}'
                )

            _metric("agent_llm_calls_today",
                    "LLM calls today per role and model", "gauge")
            for (role, model), vals in by_role_model.items():
                lines.append(
                    f'agent_llm_calls_today{{role="{role}",model="{model}"}} '
                    f'{vals["calls"]}'
                )

            _metric("agent_llm_tokens_today",
                    "LLM tokens used today per role, model, and direction", "gauge")
            for (role, model), vals in by_role_model.items():
                lines.append(
                    f'agent_llm_tokens_today{{role="{role}",model="{model}",direction="input"}} '
                    f'{vals["input_tokens"]}'
                )
                lines.append(
                    f'agent_llm_tokens_today{{role="{role}",model="{model}",direction="output"}} '
                    f'{vals["output_tokens"]}'
                )
        # Per-stack cost attribution (6.2)
        from reviewer.telemetry import read_stack_usage
        stack_recs = read_stack_usage(r, days=1)
        if stack_recs:
            _metric("agent_llm_cost_usd_today_by_stack",
                    "LLM cost in USD today attributed per tech stack", "gauge")
            for rec in stack_recs:
                lines.append(
                    f'agent_llm_cost_usd_today_by_stack{{stack="{rec["stack"]}"}} '
                    f'{round(rec["cost_usd"], 8)}'
                )
    except ImportError:
        pass  # reviewer package not installed

    return "\n".join(lines) + "\n"
