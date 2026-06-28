"""Unit tests for reviewer.gate._wait_for_ci — the CI status polling loop."""

from __future__ import annotations
from unittest.mock import MagicMock

from reviewer.gate import _wait_for_ci


class _Clock:
    """Deterministic monotonic clock advanced by the fake sleep."""
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _driver():
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.t += seconds

    return clock, sleep


def _fj(*, statuses_each=None, sequence=None):
    fj = MagicMock()
    fj.get_pr.return_value = {"head": {"sha": "deadbeef"}}
    if sequence is not None:
        fj.get_combined_status.side_effect = list(sequence)
    else:
        fj.get_combined_status.return_value = statuses_each
    return fj


def test_returns_success_after_pending():
    fj = _fj(sequence=[
        {"state": "pending", "statuses": [{"context": "CI / test"}]},
        {"state": "pending", "statuses": [{"context": "CI / test"}]},
        {"state": "success", "statuses": [{"context": "CI / test"}]},
    ])
    clock, sleep = _driver()
    res = _wait_for_ci(fj, "o", "r", 1, timeout=600, interval=5, grace=45,
                       sleep=sleep, clock=clock)
    assert res == "success"


def test_returns_failure():
    fj = _fj(statuses_each={"state": "failure", "statuses": [{"context": "CI / test"}]})
    clock, sleep = _driver()
    res = _wait_for_ci(fj, "o", "r", 1, timeout=600, interval=5, grace=45,
                       sleep=sleep, clock=clock)
    assert res == "failure"


def test_returns_none_when_no_status_within_grace():
    # No statuses ever reported -> repo has no CI workflow -> proceed
    fj = _fj(statuses_each={"state": "", "statuses": []})
    clock, sleep = _driver()
    res = _wait_for_ci(fj, "o", "r", 1, timeout=600, interval=5, grace=20,
                       sleep=sleep, clock=clock)
    assert res == "none"
    assert clock.t >= 20  # waited out the grace window


def test_returns_timeout_when_stuck_pending():
    fj = _fj(statuses_each={"state": "pending", "statuses": [{"context": "CI / test"}]})
    clock, sleep = _driver()
    res = _wait_for_ci(fj, "o", "r", 1, timeout=30, interval=5, grace=20,
                       sleep=sleep, clock=clock)
    assert res == "timeout"
    assert clock.t >= 30


def test_fetch_exception_treated_as_no_status_then_times_out():
    fj = MagicMock()
    fj.get_pr.side_effect = RuntimeError("forgejo down")
    clock, sleep = _driver()
    # Errors -> empty status; with grace < timeout it resolves to 'none'
    res = _wait_for_ci(fj, "o", "r", 1, timeout=60, interval=5, grace=15,
                       sleep=sleep, clock=clock)
    assert res == "none"
