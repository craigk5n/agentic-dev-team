"""Experiment runner (EPIC 4) — declare an experiment as data and execute it.

An experiment holds a pinned plan constant across arms (each arm = a config patch), runs
``replicates`` builds per arm, and aggregates board + telemetry + oracle + ledger into one
analysis-ready table. See spec.py for the format, runner.py for execution, aggregate.py for
results.
"""
from event_bus.experiment.spec import (
    validate_experiment, load_experiment, ExperimentSpecError,
)

__all__ = ["validate_experiment", "load_experiment", "ExperimentSpecError"]
