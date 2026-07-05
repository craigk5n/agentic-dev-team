"""
Runtime configuration store — gates, per-role model overrides, and limits, backed by Redis.

Single Redis key: "runtime_config" → JSON object.
Written by: PATCH /api/config
Read by:    GET /api/config, handlers, reviewer/gate.py
"""

from __future__ import annotations
import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis

_KEY = "runtime_config"
_GATE_FIELDS = {"pr_merge_approval", "security_signoff", "free_models_only", "plan_approval",
                "auto_escalate", "block_security_categories"}  # idea_approval is always ON
_MODEL_ROLES = {"idea", "planner", "coder", "reviewer", "tester", "security", "escalate"}


@dataclass
class GateConfig:
    idea_approval: bool = True    # always ON — read-only via API
    pr_merge_approval: bool = False
    security_signoff: bool = True
    free_models_only: bool = False  # advisory flag: restrict roles to free/local models
    plan_approval: bool = True    # ON — hold the plan for operator review before stories run
    # When ON, a story that exhausts its recode cap escalates coder+reviewer to a stronger
    # model (models.escalate) for one more round before being flagged for a human.
    auto_escalate: bool = False
    # HS-5: when ON, selected Semgrep categories (missing SRI/integrity, template XSS sinks,
    # hardcoded secrets) BLOCK the merge regardless of Semgrep's own severity. These are the
    # web-facing defects that shipped as mere warnings in the DEVHUB build. Default ON — the
    # relevant findings only occur in web code, so non-web stacks are unaffected in practice.
    block_security_categories: bool = True


@dataclass
class ModelConfig:
    """
    Per-role model overrides (litellm model strings).
    Empty string means "use the env-var default for this role".

    Examples:
      openrouter/anthropic/claude-sonnet-4-6
      anthropic/claude-opus-4-8
      openrouter/openai/gpt-4o
      ollama/mistral
    """
    idea: str = ""
    planner: str = ""
    coder: str = ""
    reviewer: str = ""
    tester: str = ""
    security: str = ""
    # Stronger model coder+reviewer escalate to when a story stalls (gates.auto_escalate).
    # Empty → the built-in default (a paid Sonnet). Must be a real API/OpenRouter model,
    # not claude-code/* (subscription is planning-only and can't run the coder fleet).
    escalate: str = ""


@dataclass
class LimitsConfig:
    """
    Per-role concurrency and rate limits.
    0 means unlimited (useful for development).

    max_concurrent_*  — max simultaneous running jobs for that role
    max_rpm_*         — max LLM calls per minute for that role
    """
    max_concurrent_coder: int = 2
    max_concurrent_reviewer: int = 3
    max_concurrent_tester: int = 2
    max_concurrent_security: int = 2
    max_concurrent_idea: int = 5
    max_concurrent_planner: int = 3

    max_rpm_coder: int = 3
    max_rpm_reviewer: int = 10
    max_rpm_tester: int = 5
    max_rpm_security: int = 5
    max_rpm_idea: int = 20
    max_rpm_planner: int = 10

    # Daily LLM spend cap in USD across all roles. 0 = unlimited. When today's
    # recorded cost reaches this, new agent work is paused (a cost backstop that
    # protects the bill even from authorized overuse or a runaway loop).
    max_cost_usd_daily: float = 0.0

    # Daily OpenRouter free-tier (:free) request cap. OpenRouter allows 50/day
    # (< $10 lifetime credit) or 1000/day (>= $10). The board warns at 80% and,
    # if hold_at_free_cap is on, parks new work at 100% so verdicts degrade
    # cleanly instead of 429-ing. 0 = unlimited / disabled.
    max_free_requests_daily: int = 1000
    hold_at_free_cap: bool = False


@dataclass
class RuntimeConfig:
    gates: GateConfig = field(default_factory=GateConfig)
    models: ModelConfig = field(default_factory=ModelConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)


def _load(r: "redis.Redis") -> RuntimeConfig:
    data = r.get(_KEY)
    if not data:
        return RuntimeConfig()
    try:
        raw = json.loads(data)
        g = raw.get("gates", {})
        m = raw.get("models", {})
        lim = raw.get("limits", {})
        # Only override gate fields present in the stored config; unset ones keep the
        # dataclass default (so a newly-added gate can't be lost like auto_escalate was).
        gates = GateConfig(**{k: bool(g[k]) for k in GateConfig.__dataclass_fields__ if k in g})
        models = ModelConfig(
            **{k: m.get(k, "") for k in ModelConfig.__dataclass_fields__}
        )
        limits = LimitsConfig(
            **{k: lim.get(k, getattr(LimitsConfig, k, 0))
               for k in LimitsConfig.__dataclass_fields__
               if k in lim}
        )
        return RuntimeConfig(gates=gates, models=models, limits=limits)
    except Exception:
        return RuntimeConfig()


def _save(r: "redis.Redis", config: RuntimeConfig) -> None:
    r.set(_KEY, json.dumps(asdict(config)))


def get_config(r: "redis.Redis") -> RuntimeConfig:
    return _load(r)


def patch_config(r: "redis.Redis", patch: dict) -> RuntimeConfig:
    """Partial update — only fields present in `patch` are changed."""
    config = _load(r)

    for key, val in patch.get("gates", {}).items():
        if key in _GATE_FIELDS:  # idea_approval is immutable
            setattr(config.gates, key, bool(val))

    for key, val in patch.get("models", {}).items():
        if key in _MODEL_ROLES:
            setattr(config.models, key, str(val))

    for key, val in patch.get("limits", {}).items():
        if hasattr(config.limits, key):
            # Coerce to the field's declared type (int counters, float for the cost cap)
            setattr(config.limits, key, type(getattr(config.limits, key))(val))

    for key, val in patch.get("project", {}).items():
        if key in _PROJECT_FIELDS:
            setattr(config.project, key, str(val))

    _save(r, config)
    return config
