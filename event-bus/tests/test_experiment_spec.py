"""Story 4.1 — experiment spec schema + validation."""
from __future__ import annotations

import json

import pytest

from event_bus import experiment
from event_bus.experiment import spec as espec


def _hs2_ab():
    return {
        "name": "hs2-ab", "pin": "idea-abc", "replicates": 3, "seed": 42,
        "arms": [
            {"name": "off", "config_patch": {"gates": {"block_security_categories": False}}},
            {"name": "on", "config_patch": {"gates": {"block_security_categories": True}}},
        ],
    }


class TestValidateExperiment:
    def test_accepts_hs2_ab(self):
        assert experiment.validate_experiment(_hs2_ab())["name"] == "hs2-ab"

    def test_accepts_model_and_limit_patches(self):
        spec = _hs2_ab()
        spec["arms"][0]["config_patch"] = {"models": {"coder": "openrouter/x"},
                                           "limits": {"max_cost_usd_daily": 5.0}}
        assert experiment.validate_experiment(spec)

    @pytest.mark.parametrize("mutate,msg", [
        (lambda s: s.pop("name"), "name"),
        (lambda s: s.pop("pin"), "pin"),
        (lambda s: s.update(arms=[]), "arms"),
        (lambda s: s.update(replicates=0), "replicates"),
        (lambda s: s.update(replicates=True), "replicates"),
    ])
    def test_rejects_missing_or_bad_fields(self, mutate, msg):
        spec = _hs2_ab()
        mutate(spec)
        with pytest.raises(espec.ExperimentSpecError, match=msg):
            experiment.validate_experiment(spec)

    def test_rejects_unknown_gate_key(self):
        spec = _hs2_ab()
        spec["arms"][0]["config_patch"] = {"gates": {"not_a_gate": True}}
        with pytest.raises(espec.ExperimentSpecError, match="unknown gates key"):
            experiment.validate_experiment(spec)

    def test_rejects_unknown_model_role(self):
        spec = _hs2_ab()
        spec["arms"][0]["config_patch"] = {"models": {"wizard": "x"}}
        with pytest.raises(espec.ExperimentSpecError, match="unknown models key"):
            experiment.validate_experiment(spec)

    def test_rejects_unknown_section(self):
        spec = _hs2_ab()
        spec["arms"][0]["config_patch"] = {"bogus": {"x": 1}}
        with pytest.raises(espec.ExperimentSpecError, match="unknown config section"):
            experiment.validate_experiment(spec)

    def test_rejects_duplicate_arm_name(self):
        spec = _hs2_ab()
        spec["arms"][1]["name"] = "off"
        with pytest.raises(espec.ExperimentSpecError, match="duplicate arm"):
            experiment.validate_experiment(spec)


class TestLoadExperiment:
    def test_load_json(self, tmp_path):
        p = tmp_path / "e.json"
        p.write_text(json.dumps(_hs2_ab()))
        assert experiment.load_experiment(str(p))["name"] == "hs2-ab"

    def test_load_yaml(self, tmp_path):
        p = tmp_path / "e.yaml"
        p.write_text("name: y\npin: idea-1\narms:\n  - name: a\n")
        assert experiment.load_experiment(str(p))["arms"][0]["name"] == "a"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(espec.ExperimentSpecError):
            experiment.load_experiment(str(tmp_path / "nope.yaml"))
