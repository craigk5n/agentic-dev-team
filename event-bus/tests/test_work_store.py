"""Tests for work_store.py — SQLite-backed work item store."""

from __future__ import annotations
import pytest

import event_bus.work_store as ws


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect the DB to a temp file and reset the module-level connection before/after each test."""
    db_file = tmp_path / "test_work_items.db"
    monkeypatch.setattr(ws, "_DB_PATH", db_file)
    # Close any existing connection and reset so get_db() opens a fresh one
    if ws._conn is not None:
        ws._conn.close()
    ws._conn = None
    yield
    # Clean up after the test
    if ws._conn is not None:
        ws._conn.close()
    ws._conn = None


# ── create_item ──────────────────────────────────────────────────────────────

class TestCreateItem:
    def test_create_idea_defaults(self):
        item = ws.create_item(item_type="idea", title="My Idea")
        assert item["id"] is not None
        assert item["type"] == "idea"
        assert item["title"] == "My Idea"
        assert item["state"] == "pending-approval"

    def test_create_story_with_state(self):
        item = ws.create_item(item_type="story", title="My Story", state="ready")
        assert item["type"] == "story"
        assert item["state"] == "ready"

    def test_create_with_all_fields(self):
        item = ws.create_item(
            item_type="idea",
            title="Full Idea",
            prompt="some prompt",
            description="some desc",
            state="pending-approval",
            model_used="gpt-4",
            repo="owner/repo",
        )
        assert item["prompt"] == "some prompt"
        assert item["description"] == "some desc"
        assert item["model_used"] == "gpt-4"
        assert item["repo"] == "owner/repo"

    def test_create_with_parent_id(self):
        idea = ws.create_item(item_type="idea", title="Parent Idea")
        story = ws.create_item(item_type="story", title="Child Story", parent_id=idea["id"])
        assert story["parent_id"] == idea["id"]

    def test_create_with_sequence(self):
        item = ws.create_item(item_type="story", title="S1", sequence=1)
        assert item["sequence"] == 1

    def test_create_with_explicit_id(self):
        item = ws.create_item(item_type="idea", title="T", item_id="custom-id-123")
        assert item["id"] == "custom-id-123"

    def test_created_and_updated_at_populated(self):
        item = ws.create_item(item_type="idea", title="T")
        assert item["created_at"] is not None
        assert item["updated_at"] is not None


# ── get_item ─────────────────────────────────────────────────────────────────

class TestGetItem:
    def test_get_existing_item(self):
        created = ws.create_item(item_type="idea", title="Test")
        fetched = ws.get_item(created["id"])
        assert fetched is not None
        assert fetched["id"] == created["id"]
        assert fetched["title"] == "Test"

    def test_get_unknown_id_returns_none(self):
        assert ws.get_item("nonexistent-id") is None

    def test_get_unknown_id_empty_string_returns_none(self):
        assert ws.get_item("") is None


# ── update_state ─────────────────────────────────────────────────────────────

class TestUpdateState:
    def test_update_changes_state(self):
        item = ws.create_item(item_type="story", title="S", state="ready")
        updated = ws.update_state(item["id"], "in-progress")
        assert updated is not None
        assert updated["state"] == "in-progress"

    def test_get_after_update_reflects_new_state(self):
        item = ws.create_item(item_type="story", title="S", state="ready")
        ws.update_state(item["id"], "in-review")
        fetched = ws.get_item(item["id"])
        assert fetched["state"] == "in-review"

    def test_updated_at_changes_on_update(self):
        item = ws.create_item(item_type="story", title="S", state="ready")
        original_updated_at = item["updated_at"]
        import time
        time.sleep(0.01)
        updated = ws.update_state(item["id"], "in-progress")
        # updated_at should be >= original (may equal if < 1ms resolution)
        assert updated["updated_at"] >= original_updated_at


# ── list_items ───────────────────────────────────────────────────────────────

class TestListItems:
    def test_list_all_returns_all_items(self):
        ws.create_item(item_type="idea", title="I1")
        ws.create_item(item_type="story", title="S1", state="ready")
        items = ws.list_items()
        assert len(items) == 2

    def test_list_with_state_filter(self):
        ws.create_item(item_type="idea", title="I1", state="pending-approval")
        ws.create_item(item_type="story", title="S1", state="ready")
        items = ws.list_items(state="ready")
        assert len(items) == 1
        assert items[0]["state"] == "ready"

    def test_list_with_type_filter(self):
        ws.create_item(item_type="idea", title="I1")
        ws.create_item(item_type="story", title="S1", state="ready")
        ideas = ws.list_items(item_type="idea")
        assert len(ideas) == 1
        assert ideas[0]["type"] == "idea"

    def test_list_with_state_and_type_filter(self):
        ws.create_item(item_type="idea", title="I1", state="pending-approval")
        ws.create_item(item_type="story", title="S1", state="ready")
        ws.create_item(item_type="story", title="S2", state="in-progress")
        items = ws.list_items(state="ready", item_type="story")
        assert len(items) == 1
        assert items[0]["title"] == "S1"

    def test_list_empty_db_returns_empty(self):
        assert ws.list_items() == []

    def test_list_state_filter_no_match_returns_empty(self):
        ws.create_item(item_type="idea", title="I1")
        assert ws.list_items(state="done") == []


# ── find_item_by_pr_url ───────────────────────────────────────────────────────

class TestFindItemByPrUrl:
    def test_matches_on_path_ignoring_host(self):
        item = ws.create_item(item_type="story", title="S", state="in-review")
        ws.set_pr_url(item["id"], "http://forgejo:3000/owner/repo/pulls/5")
        found = ws.find_item_by_pr_url("http://localhost:3000/owner/repo/pulls/5")
        assert found is not None
        assert found["id"] == item["id"]

    def test_returns_none_when_no_match(self):
        found = ws.find_item_by_pr_url("http://example.com/owner/repo/pulls/99")
        assert found is None

    def test_does_not_match_non_in_review_items(self):
        item = ws.create_item(item_type="story", title="S", state="merged")
        ws.set_pr_url(item["id"], "http://host/owner/repo/pulls/5")
        # merged state is not searched by find_item_by_pr_url
        found = ws.find_item_by_pr_url("http://other/owner/repo/pulls/5")
        assert found is None

    def test_matches_changes_requested_state(self):
        item = ws.create_item(item_type="story", title="S", state="changes-requested")
        ws.set_pr_url(item["id"], "http://host/owner/repo/pulls/10")
        found = ws.find_item_by_pr_url("http://other/owner/repo/pulls/10")
        assert found is not None


# ── unlock_next_story ─────────────────────────────────────────────────────────

class TestUnlockNextStory:
    def test_unlocks_next_sequenced_story(self):
        idea = ws.create_item(item_type="idea", title="Idea")
        s1 = ws.create_item(item_type="story", title="S1", parent_id=idea["id"], sequence=1, state="in-review")
        s2 = ws.create_item(item_type="story", title="S2", parent_id=idea["id"], sequence=2, state="backlog")
        result = ws.unlock_next_story(s1["id"])
        assert result is not None
        assert result["id"] == s2["id"]
        assert result["state"] == "ready"

    def test_returns_none_when_no_next_story(self):
        idea = ws.create_item(item_type="idea", title="Idea")
        s1 = ws.create_item(item_type="story", title="S1", parent_id=idea["id"], sequence=1, state="in-review")
        result = ws.unlock_next_story(s1["id"])
        assert result is None

    def test_returns_none_for_item_without_sequence(self):
        item = ws.create_item(item_type="story", title="S")
        assert ws.unlock_next_story(item["id"]) is None

    def test_returns_none_for_item_without_parent(self):
        item = ws.create_item(item_type="story", title="S", sequence=1)
        assert ws.unlock_next_story(item["id"]) is None

    def test_does_not_unlock_non_backlog_story(self):
        idea = ws.create_item(item_type="idea", title="Idea")
        s1 = ws.create_item(item_type="story", title="S1", parent_id=idea["id"], sequence=1, state="in-review")
        s2 = ws.create_item(item_type="story", title="S2", parent_id=idea["id"], sequence=2, state="ready")
        # S2 is already ready, not backlog — should not be found
        result = ws.unlock_next_story(s1["id"])
        assert result is None

    def test_gap_tolerant_skips_deleted_sequences(self):
        # A deleted/renumbered plan leaves a hole (e.g. seq 2-7 removed); the unlock must
        # jump to the next EXISTING backlog story, not stall looking for sequence+1.
        idea = ws.create_item(item_type="idea", title="Idea")
        s1 = ws.create_item(item_type="story", title="S1", parent_id=idea["id"], sequence=1, state="in-review")
        s8 = ws.create_item(item_type="story", title="S8", parent_id=idea["id"], sequence=8, state="backlog")
        result = ws.unlock_next_story(s1["id"])
        assert result is not None and result["id"] == s8["id"] and result["state"] == "ready"


# ── set_pr_url ────────────────────────────────────────────────────────────────

class TestSetPrUrl:
    def test_set_pr_url_updates_item(self):
        item = ws.create_item(item_type="story", title="S", state="in-progress")
        updated = ws.set_pr_url(item["id"], "http://forgejo/repo/pulls/1")
        assert updated is not None
        assert updated["pr_url"] == "http://forgejo/repo/pulls/1"

    def test_get_after_set_pr_url_reflects_value(self):
        item = ws.create_item(item_type="story", title="S", state="in-progress")
        ws.set_pr_url(item["id"], "http://forgejo/repo/pulls/2")
        fetched = ws.get_item(item["id"])
        assert fetched["pr_url"] == "http://forgejo/repo/pulls/2"


# ── set_repo ──────────────────────────────────────────────────────────────────

class TestSetRepo:
    def test_set_repo_updates_item(self):
        item = ws.create_item(item_type="idea", title="I")
        updated = ws.set_repo(item["id"], "owner/myrepo")
        assert updated is not None
        assert updated["repo"] == "owner/myrepo"

    def test_get_after_set_repo_reflects_value(self):
        item = ws.create_item(item_type="idea", title="I")
        ws.set_repo(item["id"], "owner/repo2")
        fetched = ws.get_item(item["id"])
        assert fetched["repo"] == "owner/repo2"


# ── grouped_items ─────────────────────────────────────────────────────────────

class TestGroupedItems:
    def test_groups_items_by_state(self):
        ws.create_item(item_type="idea", title="I1", state="pending-approval")
        ws.create_item(item_type="story", title="S1", state="ready")
        ws.create_item(item_type="story", title="S2", state="ready")
        groups = ws.grouped_items()
        assert "pending-approval" in groups
        assert "ready" in groups
        assert len(groups["ready"]) == 2

    def test_empty_states_not_in_result(self):
        ws.create_item(item_type="idea", title="I1")
        groups = ws.grouped_items()
        assert "ready" not in groups

    def test_empty_db_returns_empty_dict(self):
        assert ws.grouped_items() == {}


# ── get_repo_for_story ────────────────────────────────────────────────────────

class TestGetRepoForStory:
    def test_returns_story_repo_if_set(self):
        story = ws.create_item(item_type="story", title="S", state="ready", repo="owner/story-repo")
        assert ws.get_repo_for_story(story["id"]) == "owner/story-repo"

    def test_falls_back_to_parent_idea_repo(self):
        idea = ws.create_item(item_type="idea", title="I", repo="owner/idea-repo")
        story = ws.create_item(item_type="story", title="S", parent_id=idea["id"], state="ready")
        assert ws.get_repo_for_story(story["id"]) == "owner/idea-repo"

    def test_story_repo_wins_over_parent_repo(self):
        idea = ws.create_item(item_type="idea", title="I", repo="owner/idea-repo")
        story = ws.create_item(item_type="story", title="S", parent_id=idea["id"], state="ready", repo="owner/story-repo")
        assert ws.get_repo_for_story(story["id"]) == "owner/story-repo"

    def test_returns_default_if_no_repo_anywhere(self):
        story = ws.create_item(item_type="story", title="S", state="ready")
        assert ws.get_repo_for_story(story["id"], default="fallback/repo") == "fallback/repo"

    def test_returns_default_for_unknown_id(self):
        assert ws.get_repo_for_story("unknown-id", default="default/repo") == "default/repo"


# ── EPIC 2: stack / sdlc fields ───────────────────────────────────────────────

class TestStackSdlcFields:
    def test_create_with_stack_sdlc_roundtrips(self):
        from event_bus.work_store import create_item, get_item
        it = create_item(item_type="idea", title="T", stack="go", sdlc="tdd",
                         stack_rationale="fits well")
        got = get_item(it["id"])
        assert got["stack"] == "go" and got["sdlc"] == "tdd"
        assert got["stack_rationale"] == "fits well"

    def test_set_stack_sdlc_override(self):
        from event_bus.work_store import create_item, set_stack_sdlc, get_item
        it = create_item(item_type="idea", title="T", stack="python", sdlc="standard")
        set_stack_sdlc(it["id"], "go", "tdd")
        got = get_item(it["id"])
        assert got["stack"] == "go" and got["sdlc"] == "tdd"

    def test_story_inherits_stack_from_parent(self):
        from event_bus.work_store import create_item, get_stack_sdlc_for_story
        idea = create_item(item_type="idea", title="I", stack="go", sdlc="tdd")
        story = create_item(item_type="story", title="S", parent_id=idea["id"])
        assert get_stack_sdlc_for_story(story["id"]) == ("go", "tdd")


class TestStyleGuidesStore:
    def test_create_and_inherit(self):
        from event_bus.work_store import (create_item, get_style_guides_for_story,
                                          get_style_guides_for_repo, set_style_guides)
        idea = create_item(item_type="idea", title="I", style_guides=["google-python", "human-voice"])
        story = create_item(item_type="story", title="S", parent_id=idea["id"], repo="o/r")
        # story inherits the idea's guides
        assert get_style_guides_for_story(story["id"]) == ["google-python", "human-voice"]
        # repo lookup finds them
        idea2 = create_item(item_type="idea", title="I2", repo="o/r2", style_guides=["effective-go"])
        assert get_style_guides_for_repo("o/r2") == ["effective-go"]

    def test_set_override(self):
        from event_bus.work_store import create_item, set_style_guides, get_item, _parse_guides
        it = create_item(item_type="idea", title="I", style_guides=["human-voice"])
        set_style_guides(it["id"], ["google-python", "conventional-commits"])
        assert _parse_guides(get_item(it["id"])["style_guides"]) == ["google-python", "conventional-commits"]


class TestProjectLifecycle:
    def test_archive_hides_and_restore_returns_tree(self):
        idea = ws.create_item(item_type="idea", title="P", state="approved")
        s1 = ws.create_item(item_type="story", title="s1", parent_id=idea["id"], sequence=1, state="done")
        assert ws.set_archived(idea["id"], True) == 2  # idea + 1 story
        flat = [it for items in ws.grouped_items().values() for it in items]
        assert all(it["id"] not in (idea["id"], s1["id"]) for it in flat)  # off the board
        ws.set_archived(idea["id"], False)
        flat2 = [it for items in ws.grouped_items().values() for it in items]
        assert any(it["id"] == idea["id"] for it in flat2)  # restored

    def test_delete_item_tree_removes_idea_and_stories(self):
        idea = ws.create_item(item_type="idea", title="P", state="approved")
        ws.create_item(item_type="story", title="s1", parent_id=idea["id"], sequence=1, state="done")
        ws.create_item(item_type="story", title="s2", parent_id=idea["id"], sequence=2, state="done")
        assert ws.delete_item_tree(idea["id"]) == 3
        assert ws.get_item(idea["id"]) is None
        assert ws.list_items() == []

    def test_list_projects_rolls_up_story_progress(self):
        idea = ws.create_item(item_type="idea", title="P", state="approved")
        ws.create_item(item_type="story", title="s1", parent_id=idea["id"], sequence=1, state="done")
        ws.create_item(item_type="story", title="s2", parent_id=idea["id"], sequence=2, state="ready")
        p = ws.list_projects()[0]
        assert p["story_count"] == 2 and p["stories_done"] == 1 and p["archived"] is False
        ws.set_archived(idea["id"], True)
        assert ws.list_projects()[0]["archived"] is True
