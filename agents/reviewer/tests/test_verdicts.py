"""Tests for the Redis verdict store and aggregation logic."""

import json
import pytest
import fakeredis

from reviewer.verdicts import (
    ROLES,
    store_verdict,
    try_collect_all,
    try_claim_aggregation,
    store_and_check,
    aggregate_status,
    format_summary_comment,
)

REPO = "alice/backend"
PR = 7


@pytest.fixture
def r():
    return fakeredis.FakeRedis()


def _fill(r, statuses: dict):
    """Store verdicts for given role→status mapping."""
    for role, status in statuses.items():
        store_verdict(r, REPO, PR, role, {"role": role, "status": status, "summary": f"{role} {status}"})


class TestStoreVerdict:
    def test_stores_json(self, r):
        store_verdict(r, REPO, PR, "code_review", {"status": "pass"})
        data = r.get("pr_verdict:alice:backend:7:code_review")
        assert json.loads(data)["status"] == "pass"

    def test_ttl_set(self, r):
        store_verdict(r, REPO, PR, "code_review", {"status": "pass"}, ttl=100)
        ttl = r.ttl("pr_verdict:alice:backend:7:code_review")
        assert 0 < ttl <= 100


class TestTryCollectAll:
    def test_returns_none_when_incomplete(self, r):
        _fill(r, {"code_review": "pass"})
        assert try_collect_all(r, REPO, PR) is None

    def test_returns_all_when_complete(self, r):
        _fill(r, {"code_review": "pass", "test_run": "pass", "security": "pass"})
        result = try_collect_all(r, REPO, PR)
        assert result is not None
        assert set(result.keys()) == set(ROLES)

    def test_two_of_three_returns_none(self, r):
        _fill(r, {"code_review": "pass", "test_run": "fail"})
        assert try_collect_all(r, REPO, PR) is None


class TestClaimAggregation:
    def test_first_caller_wins(self, r):
        assert try_claim_aggregation(r, REPO, PR) is True

    def test_second_caller_loses(self, r):
        try_claim_aggregation(r, REPO, PR)
        assert try_claim_aggregation(r, REPO, PR) is False


class TestStoreAndCheck:
    def test_returns_none_until_all_in(self, r):
        result = store_and_check(r, REPO, PR, "code_review", {"status": "pass"})
        assert result is None
        result = store_and_check(r, REPO, PR, "test_run", {"status": "pass"})
        assert result is None

    def test_returns_all_when_third_arrives(self, r):
        store_and_check(r, REPO, PR, "code_review", {"status": "pass"})
        store_and_check(r, REPO, PR, "test_run", {"status": "pass"})
        result = store_and_check(r, REPO, PR, "security", {"status": "pass"})
        assert result is not None
        assert "code_review" in result

    def test_race_only_one_wins(self, r):
        # Simulate 3rd job calling store_and_check twice (race)
        _fill(r, {"code_review": "pass", "test_run": "pass"})
        r1 = store_and_check(r, REPO, PR, "security", {"status": "pass"})
        r2 = store_and_check(r, REPO, PR, "security", {"status": "pass"})
        assert r1 is not None
        assert r2 is None  # second call: agg lock already claimed


class TestAggregateStatus:
    def test_all_pass(self):
        verdicts = {r: {"status": "pass"} for r in ROLES}
        assert aggregate_status(verdicts) == "pass"

    def test_any_fail_dominates(self):
        verdicts = {"code_review": {"status": "pass"}, "test_run": {"status": "fail"}, "security": {"status": "warn"}}
        assert aggregate_status(verdicts) == "fail"

    def test_warn_without_fail(self):
        verdicts = {"code_review": {"status": "pass"}, "test_run": {"status": "warn"}, "security": {"status": "pass"}}
        assert aggregate_status(verdicts) == "warn"


class TestFormatSummaryComment:
    def test_pass_comment(self):
        verdicts = {r: {"status": "pass", "summary": "ok"} for r in ROLES}
        comment = format_summary_comment(verdicts)
        assert "✅" in comment
        assert "All checks passed" in comment

    def test_fail_comment(self):
        verdicts = {r: {"status": "fail", "summary": "broke"} for r in ROLES}
        comment = format_summary_comment(verdicts)
        assert "❌" in comment
        assert "failed" in comment

    def test_table_has_all_roles(self):
        verdicts = {r: {"status": "pass", "summary": "x"} for r in ROLES}
        comment = format_summary_comment(verdicts)
        assert "Code Review" in comment
        assert "Tests" in comment
        assert "Security" in comment
