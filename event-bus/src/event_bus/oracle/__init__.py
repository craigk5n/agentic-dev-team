"""Hidden acceptance oracle (EPIC 2).

An oracle is an INDEPENDENT, post-merge "does it actually work" check the coding agents
never see — it closes the CI-green ≠ works gap. It takes a built artifact/endpoint plus an
oracle spec and produces a machine-readable {passed, checks, defects} result.

Independence (Story 2.1 AC): the oracle runs *separately* from the build and CI. It never
imports, calls, or influences the agent's in-build tests or the merge gate, and oracle
specs live in the reference-project catalog (EPIC 6) — outside the repo the coder works in
— so the coder cannot read or fit to them.

Backends are registered by name (Story 2.2 = "cli" golden files; Story 2.3 = "browser"
Playwright journeys).
"""
from event_bus.oracle.spec import validate_spec, load_spec, OracleSpecError
from event_bus.oracle.runner import (
    run_oracle, register_backend, get_backend, persist_oracle_result, OracleRunError,
)

# Register default backends (Story 2.2 cli; Story 2.3 browser added later).
from event_bus.oracle.backends.cli import cli_backend as _cli_backend
register_backend("cli", _cli_backend)

__all__ = [
    "validate_spec", "load_spec", "OracleSpecError",
    "run_oracle", "register_backend", "get_backend", "persist_oracle_result",
    "OracleRunError",
]
