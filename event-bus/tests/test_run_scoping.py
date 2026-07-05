"""Story 5.1 — run/experiment scoping in the data model.

work_items gains a nullable run_id column (back-compatible); telemetry gains a per-run
rollup mirroring the per-story block. Reviewer telemetry is import-reachable here because
conftest puts agents/reviewer/src on sys.path.
"""
from __future__ import annotations

import fakeredis
import pytest

from event_bus import work_store as ws
from event_bus import telemetry as ebt
from reviewer import telemetry as tel


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    """Isolated SQLite DB per test (mirrors test_work_store.py)."""
    monkeypatch.setattr(ws, "_DB_PATH", tmp_path / "t.db")
    if ws._conn is not None:
        ws._conn.close()
    ws._conn = None
    yield
    if ws._conn is not None:
        ws._conn.close()
    ws._conn = None


class TestRunIdColumn:
    def test_create_item_stores_run_id(self):
        it = ws.create_item(item_type="story", title="S", run_id="run-1")
        assert ws.get_item(it["id"])["run_id"] == "run-1"

    def test_run_id_defaults_null_backcompat(self):
        it = ws.create_item(item_type="story", title="S")
        assert ws.get_item(it["id"])["run_id"] is None

    def test_schema_init_is_idempotent(self):
        db = ws.get_db()
        ws._init_schema(db)  # re-running migrations must not raise
        ws._init_schema(db)
        it = ws.create_item(item_type="idea", title="I")
        assert "run_id" in ws.get_item(it["id"])


class TestListItemsByRun:
    def test_returns_only_that_run(self):
        a = ws.create_item(item_type="story", title="A", run_id="run-1")
        b = ws.create_item(item_type="story", title="B", run_id="run-1")
        ws.create_item(item_type="story", title="C", run_id="run-2")
        ws.create_item(item_type="story", title="D")  # run-less
        assert {i["id"] for i in ws.list_items_by_run("run-1")} == {a["id"], b["id"]}

    def test_empty_for_unknown_run(self):
        ws.create_item(item_type="story", title="A", run_id="run-1")
        assert ws.list_items_by_run("nope") == []


class _Usage:
    def __init__(self, cost, i, o):
        self.cost, self.prompt_tokens, self.completion_tokens = cost, i, o


class _Resp:
    def __init__(self, cost, i, o):
        self.usage = _Usage(cost, i, o)


class TestRunTelemetry:
    def test_record_and_read_run_usage_sums(self):
        r = fakeredis.FakeRedis()
        tel.record_usage(r, "coder", "m", _Resp(0.5, 100, 50), run="run-1")
        tel.record_usage(r, "reviewer", "m", _Resp(0.25, 40, 20), run="run-1")
        row = next(x for x in tel.read_run_usage(r, days=1) if x["run"] == "run-1")
        assert row["cost_usd"] == pytest.approx(0.75)
        assert row["input_tokens"] == 140 and row["output_tokens"] == 70
        assert row["calls"] == 2

    def test_no_run_writes_no_run_key(self):
        r = fakeredis.FakeRedis()
        tel.record_usage(r, "coder", "m", _Resp(0.5, 100, 50))  # run omitted
        assert tel.read_run_usage(r, days=1) == []

    def test_summary_includes_by_run(self):
        r = fakeredis.FakeRedis()
        tel.record_usage(r, "coder", "m", _Resp(0.5, 100, 50), run="run-1")
        summary = ebt.get_telemetry_summary(r, days=1)
        runs = {x["run"]: x for x in summary["by_run"]}
        assert runs["run-1"]["cost_usd"] == pytest.approx(0.5)
