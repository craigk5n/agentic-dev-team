"""Experiment runner — execute arms × replicates (EPIC 4, Story 4.2).

Drives ``arms × replicates`` builds from one spec, each isolated: its own deterministic
``run_id``, its own per-run config (namespaced ``runtime_config:{run_id}`` — never touches the
global default), all replaying the same pinned plan. A build is performed by an injected
``build_fn`` (the actual idea-create + replay trigger); the runner owns orchestration,
config isolation, and failure tolerance — one failed run does not abort the batch.
"""
from __future__ import annotations

from event_bus.config_store import patch_config
from event_bus.experiment.spec import validate_experiment


def make_run_id(exp_name: str, arm_name: str, replicate: int) -> str:
    """Deterministic run id (no time/random) so a rerun reuses the same ids."""
    return f"{exp_name}--{arm_name}--r{replicate}"


def run_experiment(spec: dict, build_fn, r, *, apply_config=patch_config) -> dict:
    """Execute an experiment. ``build_fn(**run)`` performs one build and may raise; the
    runner records the failure and continues. Returns a manifest of every run."""
    spec = validate_experiment(spec)
    exp_name = spec["name"]
    pin = spec["pin"]
    seed = spec.get("seed")
    replicates = int(spec.get("replicates", 1))

    runs: list[dict] = []
    for arm in spec["arms"]:
        arm_name = arm["name"]
        patch = arm.get("config_patch") or {}
        for rep in range(replicates):
            run_id = make_run_id(exp_name, arm_name, rep)
            record = {"run_id": run_id, "arm": arm_name, "replicate": rep}
            try:
                # Isolate this arm's config under its own key (leaves the global untouched).
                if patch:
                    apply_config(r, patch, run_id=run_id)
                build_fn(run_id=run_id, arm=arm_name, replicate=rep, pin=pin, seed=seed,
                         config_patch=patch)
                record["status"] = "ok"
            except Exception as exc:  # one bad run must not abort the batch
                record["status"] = "error"
                record["error"] = str(exc)[:300]
            runs.append(record)

    return {
        "name": exp_name,
        "pin": pin,
        "replicates": replicates,
        "arms": [a["name"] for a in spec["arms"]],
        "total_runs": len(runs),
        "ok": sum(1 for x in runs if x["status"] == "ok"),
        "failed": sum(1 for x in runs if x["status"] == "error"),
        "runs": runs,
    }
