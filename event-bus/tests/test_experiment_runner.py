"""Story 4.2 — experiment runner (arms × replicates) + per-run config isolation."""
from __future__ import annotations

import fakeredis
import pytest

from event_bus import config_store as cs
from event_bus.experiment.runner import run_experiment, make_run_id


class TestPerRunConfigIsolation:
    def test_patch_writes_per_run_key_not_global(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        cs.patch_config(r, {"gates": {"plan_approval": False}}, run_id="run-1")
        assert cs.get_config(r).gates.plan_approval is True            # global untouched
        assert cs.get_config(r, run_id="run-1").gates.plan_approval is False  # per-run set

    def test_unknown_run_falls_back_to_global(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        cs.patch_config(r, {"gates": {"plan_approval": False}})  # global
        assert cs.get_config(r, run_id="never-set").gates.plan_approval is False

    def test_per_run_inherits_global_then_overrides(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        cs.patch_config(r, {"limits": {"max_cost_usd_daily": 9.0}})  # global base
        cs.patch_config(r, {"gates": {"security_signoff": False}}, run_id="run-2")
        c = cs.get_config(r, run_id="run-2")
        assert c.limits.max_cost_usd_daily == 9.0        # inherited from global
        assert c.gates.security_signoff is False          # arm override


def _spec(**kw):
    base = {"name": "exp", "pin": "idea-1", "replicates": 2, "arms": [
        {"name": "a", "config_patch": {"gates": {"plan_approval": False}}},
        {"name": "b"},
    ]}
    base.update(kw)
    return base


class TestRunExperiment:
    def test_runs_arms_times_replicates(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        calls = []
        manifest = run_experiment(_spec(), lambda **kw: calls.append(kw), r)
        assert manifest["total_runs"] == 4 and manifest["ok"] == 4 and manifest["failed"] == 0
        assert [c["run_id"] for c in calls] == [
            "exp--a--r0", "exp--a--r1", "exp--b--r0", "exp--b--r1"]
        assert calls[0]["pin"] == "idea-1"

    def test_arm_config_isolated_global_untouched(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        run_experiment(_spec(), lambda **kw: None, r)
        assert cs.get_config(r).gates.plan_approval is True                    # global intact
        assert cs.get_config(r, run_id="exp--a--r0").gates.plan_approval is False

    def test_partial_failure_does_not_abort_batch(self):
        r = fakeredis.FakeRedis(decode_responses=True)

        def build(**kw):
            if kw["arm"] == "a" and kw["replicate"] == 0:
                raise RuntimeError("boom")

        m = run_experiment(_spec(), build, r)
        assert m["ok"] == 3 and m["failed"] == 1
        err = next(x for x in m["runs"] if x["status"] == "error")
        assert err["run_id"] == "exp--a--r0" and "boom" in err["error"]

    def test_deterministic_run_ids(self):
        assert make_run_id("e", "arm", 3) == "e--arm--r3"

    def test_invalid_spec_rejected(self):
        with pytest.raises(Exception):
            run_experiment({"name": "x"}, lambda **kw: None,
                           fakeredis.FakeRedis(decode_responses=True))
