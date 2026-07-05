"""Tests for the HS-4 'no unexpected deletions' guardrail (pure logic)."""

from reviewer.deletion_guard import (
    is_refactor_intent,
    analyze_deletions,
    deletion_guardrail,
)


class TestIsRefactorIntent:
    def test_refactor_words_match(self):
        assert is_refactor_intent("Refactor the auth module")
        assert is_refactor_intent("Clean up dead code")
        assert is_refactor_intent("Remove the legacy adapter")
        assert is_refactor_intent("Migrate config to the new format")

    def test_feature_story_does_not_match(self):
        assert is_refactor_intent("Add a login form to the dashboard") is False
        assert is_refactor_intent("") is False


class TestAnalyzeDeletions:
    def test_detects_deleted_files(self):
        files = [
            {"filename": "Dockerfile", "status": "deleted", "additions": 0, "deletions": 20},
            {"filename": "app.py", "status": "modified", "additions": 10, "deletions": 2},
        ]
        a = analyze_deletions(files)
        assert a["removed"] == ["Dockerfile"]
        assert a["gutted"] == []

    def test_detects_gutted_file(self):
        # Modified file that loses 80 lines and adds back 3 → gutted.
        files = [{"filename": "core.py", "status": "modified", "additions": 3, "deletions": 80}]
        a = analyze_deletions(files)
        assert a["gutted"] == ["core.py"]

    def test_normal_edit_is_not_gutted(self):
        # A real rewrite (adds ~ deletions) is not a gut.
        files = [{"filename": "core.py", "status": "modified", "additions": 70, "deletions": 80}]
        assert analyze_deletions(files)["gutted"] == []

    def test_small_deletion_is_not_gutted(self):
        files = [{"filename": "x.py", "status": "modified", "additions": 0, "deletions": 5}]
        assert analyze_deletions(files)["gutted"] == []

    def test_added_and_renamed_ignored(self):
        files = [
            {"filename": "new.py", "status": "added", "additions": 50, "deletions": 0},
            {"filename": "b.py", "status": "renamed", "additions": 1, "deletions": 1},
        ]
        a = analyze_deletions(files)
        assert a["removed"] == [] and a["gutted"] == []

    def test_empty_and_none(self):
        assert analyze_deletions([]) == {"removed": [], "gutted": []}
        assert analyze_deletions(None) == {"removed": [], "gutted": []}


class TestDeletionGuardrail:
    def test_unexpected_deletion_blocks(self):
        files = [{"filename": "Dockerfile", "status": "deleted", "additions": 0, "deletions": 20}]
        g = deletion_guardrail(files, "Add a health endpoint")
        assert g["concern"] is True
        assert g["block"] is True
        assert "Dockerfile" in g["message"]

    def test_expected_deletion_allowed(self):
        files = [{"filename": "legacy.py", "status": "deleted", "additions": 0, "deletions": 30}]
        g = deletion_guardrail(files, "Refactor: remove the legacy adapter")
        assert g["concern"] is True
        assert g["block"] is False
        assert "allowed" in g["message"].lower()

    def test_no_deletions_no_concern(self):
        files = [{"filename": "app.py", "status": "modified", "additions": 10, "deletions": 3}]
        g = deletion_guardrail(files, "Add a feature")
        assert g == {"concern": False, "block": False, "removed": [], "gutted": [], "message": ""}
