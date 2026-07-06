"""Story 4.3 — experiment results aggregation."""
from __future__ import annotations

import csv

import fakeredis
import pytest

from event_bus import work_store as ws
from event_bus.experiment.aggregate import aggregate_experiment, write_csv


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "_DB_PATH", tmp_path / "t.db")
    if ws._conn is not None:
        ws._conn.close()
    ws._conn = None
    yield
    if ws._conn is not None:
        ws._conn.close()
    ws._conn = None


class _U:
    def __init__(self, c, i, o):
        self.cost, self.prompt_tokens, self.completion_tokens = c, i, o


class _R:
    def __init__(self, c, i, o):
        self.usage = _U(c, i, o)


def _seed_run(r, run_id, arm):
    """Create 2 stories under a run with cost, a defect, labels, and a rework flag."""
    from reviewer import telemetry as tel
    s1 = ws.create_item(item_type="story", title=f"{arm}-s1", run_id=run_id,
                        trust_boundary_class="renders-untrusted", size="m", state="done")
    s2 = ws.create_item(item_type="story", title=f"{arm}-s2", run_id=run_id,
                        trust_boundary_class="none", size="s", state="done")
    tel.record_usage(r, "coder", "m", _R(0.10, 100, 50), story=s1["id"])
    r.set(f"coder_cost_src:{s1['id']}", "json")
    r.set(f"story_reworked:{s1['id']}", "1")
    ws.add_defect(s1["id"], "not-visible", "hidden panel", source="oracle", run_id=run_id)
    return s1, s2


class TestAggregateExperiment:
    def test_row_per_run_story_with_joined_metrics(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        s1, _ = _seed_run(r, "exp--a--r0", "a")
        manifest = {"runs": [{"run_id": "exp--a--r0", "arm": "a", "status": "ok"}]}

        out = aggregate_experiment(manifest, r)
        assert len(out["rows"]) == 2
        row1 = next(x for x in out["rows"] if x["story_id"] == s1["id"])
        assert row1["arm"] == "a" and row1["trust_boundary_class"] == "renders-untrusted"
        assert row1["cost_usd"] == pytest.approx(0.10)
        assert row1["coder_cost_source"] == "json"
        assert row1["defect_count"] == 1 and row1["reworked"] is True

    def test_by_arm_summary(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        _seed_run(r, "exp--a--r0", "a")
        _seed_run(r, "exp--b--r0", "b")
        manifest = {"runs": [
            {"run_id": "exp--a--r0", "arm": "a"},
            {"run_id": "exp--b--r0", "arm": "b"}]}

        by_arm = {x["arm"]: x for x in aggregate_experiment(manifest, r)["by_arm"]}
        assert by_arm["a"]["stories"] == 2 and by_arm["a"]["defects"] == 1
        assert by_arm["a"]["rework_rate"] == 0.5   # 1 of 2 stories reworked
        assert by_arm["a"]["cost_usd"] == pytest.approx(0.10)

    def test_empty_manifest_is_safe(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        out = aggregate_experiment({"runs": []}, r)
        assert out["rows"] == [] and out["by_arm"] == []

    def test_write_csv_round_trip(self, tmp_path):
        r = fakeredis.FakeRedis(decode_responses=True)
        _seed_run(r, "exp--a--r0", "a")
        rows = aggregate_experiment({"runs": [{"run_id": "exp--a--r0", "arm": "a"}]}, r)["rows"]
        p = tmp_path / "out.csv"
        write_csv(rows, p)
        loaded = list(csv.DictReader(p.open()))
        assert len(loaded) == 2 and loaded[0]["arm"] == "a"
