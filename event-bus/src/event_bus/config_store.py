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
_GATE_FIELDS = {"pr_merge_approval", "security_signoff", "free_models_only"}  # idea_approval is always ON
_MODEL_ROLES = {"idea", "planner", "coder", "reviewer", "tester", "security"}


@dataclass
class GateConfig:
    idea_approval: bool = True    # always ON — read-only via API
    pr_merge_approval: bool = False
    security_signoff: bool = True
    free_models_only: bool = False  # advisory flag: restrict roles to free/local models


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
        gates = GateConfig(
            idea_approval=g.get("idea_approval", True),
            pr_merge_approval=g.get("pr_merge_approval", False),
            security_signoff=g.get("security_signoff", True),
            free_models_only=g.get("free_models_only", False),
        )
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
            setattr(config.limits, key, int(val))

    _save(r, config)
    return config
