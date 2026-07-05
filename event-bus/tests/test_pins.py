"""Story 1.1 — serialize a planner decomposition to experiments/pins/.

Unit-level tests of the pin envelope + file writer, plus an integration test proving
_persist_plan writes a pin as a pure side effect (never affecting build behavior).
"""
from __future__ import annotations

import json
import types

import pytest

from event_bus import pins
from event_bus import main as m
from event_bus.config import settings
from event_bus.config_store import RuntimeConfig, GateConfig


CANNED_PLAN = {
    "project_name": "Widget",
    "module_name": "Widget",
    "epics": [{"name": "Core", "description": "core epic"}],
    "stories": [
        {"title": "Story one", "description": "do one", "priority": "high", "epic": "Core"},
        {"title": "Story two", "description": "do two", "priority": "medium", "epic": "Core"},
    ],
}


class TestBuildPin:
    def test_wraps_plan_with_version_and_model(self):
        pin = pins.build_pin("idea-1", CANNED_PLAN, "openrouter/anthropic/claude-sonnet-4-6")
        assert pin["version"] == pins.PIN_SCHEMA_VERSION
        assert pin["item_id"] == "idea-1"
        assert pin["planner_model"] == "openrouter/anthropic/claude-sonnet-4-6"
        assert pin["plan"] == CANNED_PLAN  # stored verbatim

    def test_missing_model_normalizes_to_empty_string(self):
        assert pins.build_pin("idea-1", CANNED_PLAN, "")["planner_model"] == ""
        assert pins.build_pin("idea-1", CANNED_PLAN, None)["planner_model"] == ""


class TestWritePin:
    def test_writes_json_artifact_with_envelope(self, tmp_path):
        path = pins.write_pin("idea-1", CANNED_PLAN, "model-x", base_dir=tmp_path)
        assert path == tmp_path / "idea-1.json"
        loaded = json.loads(path.read_text())
        assert loaded["version"] == pins.PIN_SCHEMA_VERSION
        assert loaded["planner_model"] == "model-x"
        assert loaded["plan"]["stories"] == CANNED_PLAN["stories"]
        assert loaded["plan"]["epics"] == CANNED_PLAN["epics"]

    def test_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "experiments" / "pins"
        path = pins.write_pin("idea-2", CANNED_PLAN, "m", base_dir=nested)
        assert path.exists() and path.parent == nested

    def test_overwrite_is_atomic_and_leaves_no_tmp(self, tmp_path):
        pins.write_pin("idea-3", CANNED_PLAN, "m", base_dir=tmp_path)
        pins.write_pin("idea-3", CANNED_PLAN, "m", base_dir=tmp_path)
        assert (tmp_path / "idea-3.json").exists()
        assert not list(tmp_path.glob("*.tmp"))

    def test_default_base_dir_reads_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "pins_dir", str(tmp_path / "pd"))
        path = pins.write_pin("idea-4", CANNED_PLAN, "m")
        assert path == tmp_path / "pd" / "idea-4.json" and path.exists()


def _stub_store(monkeypatch):
    """Patch the work-store + item lookup so _persist_plan needs no DB/async."""
    monkeypatch.setattr(m, "get_item", lambda _id: {})
    created: list[dict] = []

    def _fake_create(**kw):
        created.append(kw)
        return {"id": f"s{len(created)}", "title": kw["title"],
                "description": kw.get("description", "")}

    monkeypatch.setattr(m, "create_item", _fake_create)
    return created


_STACK = types.SimpleNamespace(id="python")
_SDLC = types.SimpleNamespace(id="standard")
_GATED = RuntimeConfig(gates=GateConfig(plan_approval=True))  # park stories, no auto-trigger


class TestPersistPlanWritesPin:
    def test_pin_written_as_side_effect(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "pins_dir", str(tmp_path))
        created = _stub_store(monkeypatch)

        m._persist_plan("idea-9", CANNED_PLAN, "dev/widget", _STACK, _SDLC, "model-x", _GATED)

        loaded = json.loads((tmp_path / "idea-9.json").read_text())
        assert loaded["plan"]["stories"] == CANNED_PLAN["stories"]
        assert loaded["planner_model"] == "model-x"
        # side-effect only: stories still persisted exactly as before
        assert [c["title"] for c in created] == ["Story one", "Story two"]

    def test_pin_failure_never_breaks_the_build(self, tmp_path, monkeypatch):
        created = _stub_store(monkeypatch)
        monkeypatch.setattr("event_bus.pins.write_pin",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

        m._persist_plan("idea-10", CANNED_PLAN, "dev/widget", _STACK, _SDLC, "model-x", _GATED)

        assert len(created) == 2  # build proceeded despite pin failure

    def test_resume_append_does_not_overwrite_pin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "pins_dir", str(tmp_path))
        _stub_store(monkeypatch)

        m._persist_plan("idea-11", CANNED_PLAN, "dev/widget", _STACK, _SDLC, "model-x",
                        _GATED, seq_offset=5)

        assert not (tmp_path / "idea-11.json").exists()
