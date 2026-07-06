"""Results aggregation (EPIC 4, Story 4.3).

Joins the board (stories + trust-boundary/size labels), telemetry (per-story cost +
provenance), and the defect ledger on ``run_id`` into one analysis-ready table: one row per
(run_id, story) plus a per-arm summary. Rows are flat dicts — ``pandas.DataFrame(rows)`` loads
them directly; ``write_csv`` dumps them for anything else.
"""
from __future__ import annotations

import csv

from event_bus.work_store import list_items_by_run, list_defects


def _redis_str(r, key: str) -> str | None:
    try:
        v = r.get(key)
        if isinstance(v, (bytes, bytearray)):
            return v.decode()
        return v
    except Exception:
        return None


def aggregate_experiment(manifest: dict, r, *, days: int = 30) -> dict:
    """Aggregate an experiment's runs into {rows, by_arm}. ``manifest`` is the run_experiment
    output (maps run_id → arm)."""
    run_arm = {run["run_id"]: run["arm"] for run in manifest.get("runs", [])}

    try:
        from reviewer.telemetry import read_story_usage
        cost_by_story = {x["story"]: x for x in read_story_usage(r, days=days)}
    except Exception:
        cost_by_story = {}

    rows: list[dict] = []
    for run_id, arm in run_arm.items():
        defects_by_story: dict[str, int] = {}
        for d in list_defects(run_id=run_id):
            if d.get("story_id"):
                defects_by_story[d["story_id"]] = defects_by_story.get(d["story_id"], 0) + 1
        for s in list_items_by_run(run_id):
            sid = s["id"]
            c = cost_by_story.get(sid, {})
            rows.append({
                "run_id": run_id, "arm": arm, "story_id": sid,
                "title": s.get("title"), "epic": s.get("epic"),
                "trust_boundary_class": s.get("trust_boundary_class"),
                "size": s.get("size"), "state": s.get("state"),
                "cost_usd": round(c.get("cost_usd", 0.0), 6),
                "input_tokens": c.get("input_tokens", 0),
                "output_tokens": c.get("output_tokens", 0),
                "coder_cost_source": _redis_str(r, f"coder_cost_src:{sid}") or "unknown",
                "defect_count": defects_by_story.get(sid, 0),
                "reworked": bool(_redis_str(r, f"story_reworked:{sid}")),
            })

    by_arm: dict[str, dict] = {}
    for row in rows:
        a = by_arm.setdefault(row["arm"], {
            "arm": row["arm"], "stories": 0, "reworked": 0, "defects": 0, "cost_usd": 0.0})
        a["stories"] += 1
        a["reworked"] += 1 if row["reworked"] else 0
        a["defects"] += row["defect_count"]
        a["cost_usd"] += row["cost_usd"]
    for a in by_arm.values():
        a["rework_rate"] = round(a["reworked"] / a["stories"], 4) if a["stories"] else 0.0
        a["cost_usd"] = round(a["cost_usd"], 6)

    return {"rows": rows, "by_arm": list(by_arm.values())}


_FIELDS = ["run_id", "arm", "story_id", "title", "epic", "trust_boundary_class", "size",
           "state", "cost_usd", "input_tokens", "output_tokens", "coder_cost_source",
           "defect_count", "reworked"]


def write_csv(rows: list[dict], path) -> None:
    """Write aggregated rows to a CSV (analysis-ready)."""
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in _FIELDS})
