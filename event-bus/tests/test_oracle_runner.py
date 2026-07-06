"""Story 2.1 — oracle contract & runner skeleton."""
from __future__ import annotations

import json

import fakeredis
import pytest

from event_bus import oracle
from event_bus.oracle import spec as ospec
from event_bus.oracle import runner as orunner


def _spec(backend="cli"):
    return {"name": "demo", "backend": backend, "checks": [{"name": "c1"}]}


class TestValidateSpec:
    def test_accepts_well_formed(self):
        assert oracle.validate_spec(_spec()) == _spec()

    @pytest.mark.parametrize("bad", [
        {},                                                   # not a spec
        {"backend": "cli", "checks": [{}]},                   # no name
        {"name": "x", "backend": "nope", "checks": [{}]},     # bad backend
        {"name": "x", "backend": "cli", "checks": []},        # empty checks
        {"name": "x", "backend": "cli"},                      # no checks
    ])
    def test_rejects_malformed(self, bad):
        with pytest.raises(ospec.OracleSpecError):
            oracle.validate_spec(bad)

    def test_load_spec_from_file(self, tmp_path):
        p = tmp_path / "o.json"
        p.write_text(json.dumps(_spec("browser")))
        assert oracle.load_spec(str(p))["backend"] == "browser"

    def test_load_missing_raises(self, tmp_path):
        with pytest.raises(ospec.OracleSpecError):
            oracle.load_spec(str(tmp_path / "nope.json"))


class TestRunOracle:
    def test_pass_when_all_checks_pass(self):
        backends = {"cli": lambda s, t: ([{"name": "c1", "passed": True}], [])}
        res = oracle.run_oracle(_spec(), {"binary": "/x"}, backends=backends)
        assert res["passed"] is True
        assert res["name"] == "demo" and res["backend"] == "cli"
        assert res["defects"] == []

    def test_fail_when_a_check_fails(self):
        backends = {"cli": lambda s, t: (
            [{"name": "c1", "passed": False, "detail": "stdout mismatch"}],
            [{"class": "output-mismatch", "description": "expected X got Y"}])}
        res = oracle.run_oracle(_spec(), {}, backends=backends)
        assert res["passed"] is False
        assert res["defects"][0]["class"] == "output-mismatch"

    def test_fail_on_defect_even_if_checks_pass(self):
        backends = {"cli": lambda s, t: ([{"name": "c1", "passed": True}],
                                         [{"class": "runtime", "description": "500 on load"}])}
        assert oracle.run_oracle(_spec(), {}, backends=backends)["passed"] is False

    def test_unknown_backend_raises(self):
        with pytest.raises(orunner.OracleRunError):
            oracle.run_oracle(_spec(), {}, backends={})

    def test_invalid_spec_raises_before_backend(self):
        with pytest.raises(ospec.OracleSpecError):
            oracle.run_oracle({"name": "x", "backend": "cli", "checks": []}, {}, backends={})

    def test_register_and_get_backend(self):
        fn = lambda s, t: ([{"name": "c", "passed": True}], [])
        oracle.register_backend("custom", fn)  # not "cli" — don't clobber the default
        assert oracle.get_backend("custom") is fn


class TestPersistOracleResult:
    def test_persists_linked_to_story_and_run(self):
        r = fakeredis.FakeRedis(decode_responses=True)
        result = {"name": "demo", "backend": "cli", "passed": False,
                  "checks": [], "defects": [{"class": "x", "description": "y"}]}
        payload = oracle.persist_oracle_result(r, "story-1", result, run_id="run-9")
        assert payload["story_id"] == "story-1" and payload["run_id"] == "run-9"
        stored = json.loads(r.get("oracle:result:story-1"))
        assert stored["passed"] is False and stored["defects"][0]["class"] == "x"

    def test_none_redis_is_safe(self):
        payload = oracle.persist_oracle_result(None, "s", {"passed": True})
        assert payload["story_id"] == "s"
