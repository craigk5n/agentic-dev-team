"""Story 1.1 — serialize a planner decomposition to experiments/pins/.

Unit-level tests of the pin envelope + file writer, plus an integration test proving
_persist_plan writes a pin as a pure side effect (never affecting build behavior).
"""
from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock

import fakeredis
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

    def test_persist_plan_forwards_story_labels(self, tmp_path, monkeypatch):
        # Story 5.2: trust-boundary/size labels on pin stories flow into create_item.
        monkeypatch.setattr(settings, "pins_dir", str(tmp_path))
        created = _stub_store(monkeypatch)
        plan = {**CANNED_PLAN, "stories": [
            {**CANNED_PLAN["stories"][0], "trust_boundary_class": "generates-code", "size": "l"},
            CANNED_PLAN["stories"][1],
        ]}
        m._persist_plan("idea-lab", plan, "dev/w", _STACK, _SDLC, "model-x", _GATED)
        assert created[0]["trust_boundary_class"] == "generates-code"
        assert created[0]["size"] == "l"
        assert created[1]["trust_boundary_class"] == ""  # unset when absent


# ── Story 1.2: load / validate / replay ────────────────────────────────────────

class TestLoadPin:
    def test_roundtrip_by_id(self, tmp_path):
        pins.write_pin("idea-1", CANNED_PLAN, "m", base_dir=tmp_path)
        assert pins.load_pin("idea-1", base_dir=tmp_path) == CANNED_PLAN

    def test_roundtrip_by_path(self, tmp_path):
        path = pins.write_pin("idea-1", CANNED_PLAN, "m", base_dir=tmp_path)
        assert pins.load_pin(str(path)) == CANNED_PLAN

    def test_missing_pin_raises(self, tmp_path):
        with pytest.raises(pins.PinError):
            pins.load_pin("nope", base_dir=tmp_path)

    def test_wrong_version_rejected(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"version": 999, "plan": CANNED_PLAN}))
        with pytest.raises(pins.PinError, match="version"):
            pins.load_pin(str(p))

    def test_empty_stories_rejected(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"version": pins.PIN_SCHEMA_VERSION,
                                 "plan": {"stories": []}}))
        with pytest.raises(pins.PinError, match="stories"):
            pins.load_pin(str(p))

    def test_story_without_title_rejected(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"version": pins.PIN_SCHEMA_VERSION,
                                 "plan": {"stories": [{"description": "x"}]}}))
        with pytest.raises(pins.PinError, match="title"):
            pins.load_pin(str(p))

    def test_oversized_pin_rejected(self, tmp_path, monkeypatch):
        path = pins.write_pin("big", CANNED_PLAN, "m", base_dir=tmp_path)
        monkeypatch.setattr(pins, "MAX_PIN_BYTES", 10)
        with pytest.raises(pins.PinError, match="large"):
            pins.load_pin(str(path))


class TestRebaseReplayPlan:
    def test_rebases_repo_prefix_only(self):
        plan = {"stories": [
            {"title": "S1", "description": "repo: dev/old\ndo the thing", "epic": "E"},
            {"title": "S2", "description": "no prefix here", "epic": "E"},
        ]}
        out = m._rebase_replay_plan(plan, "dev/new")
        assert out["stories"][0]["description"] == "repo: dev/new\ndo the thing"
        assert out["stories"][1]["description"] == "no prefix here"  # untouched
        assert [s["title"] for s in out["stories"]] == ["S1", "S2"]  # tree unchanged
        # immutability: original not mutated
        assert plan["stories"][0]["description"] == "repo: dev/old\ndo the thing"


def _replay_env(monkeypatch, repo="dev/armA"):
    """Wire fakeredis + patch the non-planning prerequisites of _run_planner."""
    r = fakeredis.FakeRedis()
    monkeypatch.setattr(m, "_redis_conn", r)
    monkeypatch.setattr(m, "over_budget", lambda *a, **k: False)
    monkeypatch.setattr(m, "get_item", lambda _id: {})
    monkeypatch.setattr(m, "set_repo", lambda *a, **k: None)
    monkeypatch.setattr(m, "get_config", lambda *a, **k: _GATED)
    monkeypatch.setattr(m, "_provision_project_repo", lambda *a, **k: repo)
    created: list[dict] = []
    monkeypatch.setattr(m, "create_item",
                        lambda **kw: created.append(kw) or {"id": f"s{len(created)}",
                                                             "title": kw["title"],
                                                             "description": kw.get("description", "")})
    return r, created


class TestReplayInRunPlanner:
    def test_replay_makes_no_planner_llm_call(self, monkeypatch):
        r, created = _replay_env(monkeypatch)
        pins.write_pin("src", CANNED_PLAN, "m")           # into isolated pins_dir
        r.set("plan_replay:armA", "src")                  # designate replay source
        planner = types.SimpleNamespace(run_planner=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("planner LLM must not run in replay")))
        import sys
        monkeypatch.setitem(sys.modules, "planner_agent.main", planner)

        asyncio.run(m._run_planner("armA", "Widget", "desc"))

        assert [c["title"] for c in created] == ["Story one", "Story two"]
        assert all(c["parent_id"] == "armA" for c in created)

    def test_replay_identical_tree_across_repos(self, monkeypatch):
        # Arm A
        r, created = _replay_env(monkeypatch, repo="dev/armA")
        repo_plan = {**CANNED_PLAN, "stories": [
            {**s, "description": f"repo: dev/orig\n{s['description']}"}
            for s in CANNED_PLAN["stories"]]}
        pins.write_pin("src", repo_plan, "m")
        r.set("plan_replay:armA", "src")
        asyncio.run(m._run_planner("armA", "W", "d"))
        arm_a = list(created)

        # Arm B — same pin, different provisioned repo
        r2, created_b = _replay_env(monkeypatch, repo="dev/armB")
        pins.write_pin("src", repo_plan, "m")
        r2.set("plan_replay:armB", "src")
        asyncio.run(m._run_planner("armB", "W", "d"))

        assert [c["title"] for c in arm_a] == [c["title"] for c in created_b]
        assert [c["epic"] for c in arm_a] == [c["epic"] for c in created_b]
        assert [c["sequence"] for c in arm_a] == [c["sequence"] for c in created_b]
        # repo differs per arm; description repo-prefix rebased accordingly
        assert arm_a[0]["repo"] == "dev/armA" and created_b[0]["repo"] == "dev/armB"
        assert arm_a[0]["description"].startswith("repo: dev/armA")
        assert created_b[0]["description"].startswith("repo: dev/armB")

    def test_replay_load_failure_aborts_without_fallback(self, monkeypatch):
        r, created = _replay_env(monkeypatch)
        r.set("plan_replay:armA", "does-not-exist")       # bad source
        called = {"planner": False}

        def _boom(*a, **k):
            called["planner"] = True
            return {}
        import sys
        monkeypatch.setitem(sys.modules, "planner_agent.main",
                            types.SimpleNamespace(run_planner=_boom))

        asyncio.run(m._run_planner("armA", "W", "d"))

        assert called["planner"] is False   # no silent fall back to fresh planning
        assert created == []                 # nothing persisted


# ── Story 1.3: operator control to select replay ───────────────────────────────

_IDEA = {"id": "armX", "type": "idea", "title": "W", "description": "d",
         "state": "pending-approval"}


class TestReplayEndpoint:
    def test_happy_path_sets_marker_and_triggers_planner(self, client, monkeypatch):
        pins.write_pin("srcpin", CANNED_PLAN, "m")           # into isolated pins_dir
        monkeypatch.setattr(m, "get_item", lambda _id: dict(_IDEA))
        monkeypatch.setattr(m, "update_state", lambda *a, **k: None)
        monkeypatch.setattr(m, "_run_planner", AsyncMock())

        resp = client.post("/api/items/armX/replay", json={"pin": "srcpin"})

        assert resp.status_code == 202
        assert resp.json()["pin"] == "srcpin"
        m._redis_conn.set.assert_any_call("plan_replay:armX", "srcpin")
        m._run_planner.assert_called_once()

    def test_invalid_pin_rejected_422(self, client, monkeypatch):
        monkeypatch.setattr(m, "get_item", lambda _id: dict(_IDEA))
        monkeypatch.setattr(m, "_run_planner", AsyncMock())
        resp = client.post("/api/items/armX/replay", json={"pin": "does-not-exist"})
        assert resp.status_code == 422
        assert "invalid pin" in resp.json()["detail"]
        m._run_planner.assert_not_called()

    def test_missing_pin_rejected_422(self, client, monkeypatch):
        monkeypatch.setattr(m, "get_item", lambda _id: dict(_IDEA))
        resp = client.post("/api/items/armX/replay", json={})
        assert resp.status_code == 422

    def test_unknown_item_404(self, client, monkeypatch):
        monkeypatch.setattr(m, "get_item", lambda _id: None)
        resp = client.post("/api/items/none/replay", json={"pin": "x"})
        assert resp.status_code == 404

    def test_non_idea_rejected_409(self, client, monkeypatch):
        monkeypatch.setattr(m, "get_item", lambda _id: {"id": "s1", "type": "story"})
        resp = client.post("/api/items/s1/replay", json={"pin": "x"})
        assert resp.status_code == 409

    def test_provenance_surfaced_on_get(self, client, monkeypatch):
        monkeypatch.setattr(m, "get_item", lambda _id: {"id": "armX", "type": "idea"})
        m._redis_conn.get.return_value = b"srcpin"
        resp = client.get("/api/items/armX")
        assert resp.status_code == 200
        assert resp.json().get("replay_pin") == "srcpin"
