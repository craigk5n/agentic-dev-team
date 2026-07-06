"""Experiment spec format + validation (EPIC 4, Story 4.1).

An experiment is declared as data (YAML or JSON):

    name: hs2-checklist-ab
    pin: idea-abc123          # pinned plan ref (item id or path) — same plan for every arm
    replicates: 3             # builds per arm
    seed: 42                  # optional; threaded into LLM calls where honored
    metrics: [rework_rate, cost_usd, oracle_defects]   # optional (advisory)
    arms:
      - name: checklist-off
        config_patch:
          gates: { block_security_categories: false }
      - name: checklist-on
        config_patch:
          gates: { block_security_categories: true }

Each arm's ``config_patch`` is validated against the REAL allowed gate/model/limit keys from
config_store, so a typo'd knob fails fast instead of silently no-op'ing a whole arm.
"""
from __future__ import annotations

import json
from pathlib import Path

from event_bus.config_store import _GATE_FIELDS, _MODEL_ROLES, LimitsConfig

_LIMIT_FIELDS = set(LimitsConfig.__dataclass_fields__)
_PATCH_SECTIONS = {"gates": _GATE_FIELDS, "models": _MODEL_ROLES, "limits": _LIMIT_FIELDS}


class ExperimentSpecError(Exception):
    """An experiment spec is missing required fields or names an unknown config knob."""


def _validate_config_patch(patch: dict, arm_name: str) -> None:
    if not isinstance(patch, dict):
        raise ExperimentSpecError(f"arm {arm_name!r}: config_patch must be an object")
    unknown_sections = set(patch) - set(_PATCH_SECTIONS)
    if unknown_sections:
        raise ExperimentSpecError(
            f"arm {arm_name!r}: unknown config section(s) {sorted(unknown_sections)}")
    for section, allowed in _PATCH_SECTIONS.items():
        for key in (patch.get(section) or {}):
            if key not in allowed:
                raise ExperimentSpecError(
                    f"arm {arm_name!r}: unknown {section} key {key!r}")


def validate_experiment(spec: dict) -> dict:
    """Validate an experiment spec; raise ExperimentSpecError on any problem. Returns it."""
    if not isinstance(spec, dict):
        raise ExperimentSpecError("experiment spec must be an object")
    if not spec.get("name"):
        raise ExperimentSpecError("experiment spec requires a 'name'")
    if not spec.get("pin"):
        raise ExperimentSpecError("experiment spec requires a 'pin' (pinned plan ref)")
    reps = spec.get("replicates", 1)
    if not isinstance(reps, int) or isinstance(reps, bool) or reps < 1:
        raise ExperimentSpecError("'replicates' must be a positive integer")
    arms = spec.get("arms")
    if not isinstance(arms, list) or not arms:
        raise ExperimentSpecError("experiment spec requires a non-empty 'arms' list")
    seen: set[str] = set()
    for arm in arms:
        if not isinstance(arm, dict) or not arm.get("name"):
            raise ExperimentSpecError("each arm requires a 'name'")
        if arm["name"] in seen:
            raise ExperimentSpecError(f"duplicate arm name {arm['name']!r}")
        seen.add(arm["name"])
        _validate_config_patch(arm.get("config_patch") or {}, arm["name"])
    return spec


def load_experiment(path: str | Path) -> dict:
    """Read + validate an experiment spec (YAML or JSON) from disk."""
    p = Path(path)
    if not p.is_file():
        raise ExperimentSpecError(f"experiment spec not found: {p}")
    try:
        text = p.read_text()
        if p.suffix in (".yaml", ".yml"):
            import yaml
            spec = yaml.safe_load(text)
        else:
            spec = json.loads(text)
    except ExperimentSpecError:
        raise
    except Exception as exc:
        raise ExperimentSpecError(f"experiment spec unreadable: {exc}") from exc
    return validate_experiment(spec)
