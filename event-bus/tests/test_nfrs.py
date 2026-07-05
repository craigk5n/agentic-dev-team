"""Tests for HS-7 NFR capture + enforcement."""

from __future__ import annotations
import pytest

import event_bus.nfrs as n
import event_bus.work_store as ws


class TestNfrRegistry:
    def test_detect_local_first(self):
        assert n.detect_nfrs("A local-first, self-hosted MCP hub for the homelab") == ["local-first"]

    def test_detect_offline(self):
        assert "offline-capable" in n.detect_nfrs("must work fully offline, no internet")

    def test_detect_none_for_saas(self):
        assert n.detect_nfrs("A public multi-tenant SaaS dashboard") == []

    def test_is_known(self):
        assert n.is_known("local-first") and not n.is_known("bogus")

    def test_reconciliation_note_mentions_cdn_and_ssrf(self):
        note = n.reconciliation_note(["local-first"]).lower()
        assert "cdn" in note and ("ssrf" in note or "lan" in note or "loopback" in note)

    def test_assertions_present(self):
        a = n.assertions(["local-first"]).lower()
        assert "external network" in a or "cdn" in a

    def test_empty_ids_give_empty_strings(self):
        assert n.reconciliation_note([]) == "" and n.assertions([]) == ""
        assert n.reconciliation_note(["bogus"]) == ""


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test_nfr_work_items.db"
    monkeypatch.setattr(ws, "_DB_PATH", db_file)
    if ws._conn is not None:
        ws._conn.close()
    ws._conn = None
    yield
    if ws._conn is not None:
        ws._conn.close()
    ws._conn = None


class TestNfrPersistence:
    def test_set_and_get_nfrs_on_idea(self):
        idea = ws.create_item(item_type="idea", title="Local hub")
        ws.set_planning_inputs(idea["id"], nfrs="local-first,offline-capable")
        assert ws.get_nfrs_for_story(idea["id"]) == ["local-first", "offline-capable"]

    def test_story_inherits_nfrs_from_parent_idea(self):
        idea = ws.create_item(item_type="idea", title="Local hub")
        ws.set_planning_inputs(idea["id"], nfrs="local-first")
        story = ws.create_item(item_type="story", title="Build UI", parent_id=idea["id"])
        assert ws.get_nfrs_for_story(story["id"]) == ["local-first"]

    def test_get_nfrs_for_repo(self):
        idea = ws.create_item(item_type="idea", title="Local hub", repo="acme/hub")
        ws.set_planning_inputs(idea["id"], nfrs="local-first")
        assert ws.get_nfrs_for_repo("acme/hub") == ["local-first"]

    def test_no_nfrs_is_empty(self):
        idea = ws.create_item(item_type="idea", title="Plain")
        assert ws.get_nfrs_for_story(idea["id"]) == []
        assert ws.get_nfrs_for_repo("nobody/nothing") == []
