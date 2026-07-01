"""Tests for the stack & SDLC catalog (EPIC 1)."""

import textwrap

import pytest

from event_bus.catalog import Catalog, get_catalog
from event_bus.catalog.loader import load_sdlc, load_stacks
from event_bus.catalog.schema import SdlcStyle, StackDefinition


# ── schema (Story 1.1 / 1.2) ──────────────────────────────────────────────────

class TestSchema:
    def test_valid_stack(self):
        s = StackDefinition(id="x", display_name="X", ci_image="i", coder_image="c",
                            ci_workflow="name: CI")
        assert s.id == "x" and s.default_sdlc == "standard" and s.scaffold == {}

    def test_rejects_unknown_field(self):
        with pytest.raises(Exception):
            StackDefinition(id="x", display_name="X", ci_image="i", coder_image="c",
                            ci_workflow="w", bogus="nope")

    def test_rejects_bad_id(self):
        with pytest.raises(Exception):
            StackDefinition(id="Bad ID", display_name="X", ci_image="i",
                            coder_image="c", ci_workflow="w")

    def test_rejects_absolute_scaffold_path(self):
        with pytest.raises(Exception):
            StackDefinition(id="x", display_name="X", ci_image="i", coder_image="c",
                            ci_workflow="w", scaffold={"/etc/passwd": "x"})

    def test_rejects_traversal_scaffold_path(self):
        with pytest.raises(Exception):
            StackDefinition(id="x", display_name="X", ci_image="i", coder_image="c",
                            ci_workflow="w", scaffold={"../escape": "x"})

    def test_valid_sdlc(self):
        s = SdlcStyle(id="tdd", display_name="TDD", planner_directive="tests first")
        assert s.id == "tdd"


# ── loader (Story 1.1 / 1.2) ──────────────────────────────────────────────────

class TestLoader:
    def _write(self, base, kind, name, body):
        d = base / kind
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(textwrap.dedent(body))

    def test_loads_from_dir(self, tmp_path):
        self._write(tmp_path, "stacks", "rust.yaml", """
            id: rust
            display_name: Rust
            ci_image: rust:1
            coder_image: dev/coder-rust
            ci_workflow: "name: CI"
        """)
        stacks = load_stacks([tmp_path])
        assert "rust" in stacks and stacks["rust"].display_name == "Rust"

    def test_malformed_definition_skipped(self, tmp_path):
        self._write(tmp_path, "stacks", "good.yaml", """
            id: good
            display_name: Good
            ci_image: i
            coder_image: c
            ci_workflow: w
        """)
        self._write(tmp_path, "stacks", "bad.yaml", "id: bad\nthis: is missing required fields\n")
        stacks = load_stacks([tmp_path])
        assert "good" in stacks and "bad" not in stacks  # one bad def doesn't break the rest

    def test_user_dir_overrides_by_id(self, tmp_path):
        base, user = tmp_path / "a", tmp_path / "b"
        self._write(base, "stacks", "go.yaml", """
            id: go
            display_name: Go base
            ci_image: i
            coder_image: c
            ci_workflow: w
        """)
        self._write(user, "stacks", "go.yaml", """
            id: go
            display_name: Go override
            ci_image: i
            coder_image: c
            ci_workflow: w
        """)
        stacks = load_stacks([base, user])
        assert stacks["go"].display_name == "Go override"


# ── shipped defaults (Story 1.3 / 1.4) ────────────────────────────────────────

class TestDefaults:
    def test_default_stacks_present(self):
        ids = set(load_stacks().keys())
        assert {"python", "node-ts", "go", "generic", "rust"} <= ids

    def test_rust_stack_shape(self):
        rs = load_stacks()["rust"]
        assert rs.display_name == "Rust"
        assert rs.test_command == "cargo test"
        assert "cargo test" in rs.ci_workflow
        assert "Cargo.toml" in rs.scaffold and "src/lib.rs" in rs.scaffold
        assert rs.coder_image == "dev-agents/coder-rust:latest"
        assert rs.recode_cap == 5  # stricter CI (fmt+clippy+doc-tests) gets more attempts

    def test_recode_cap_defaults_to_zero(self):
        # Stacks that don't set a cap use 0 (→ the orchestrator's global default).
        assert load_stacks()["python"].recode_cap == 0

    def test_ci_push_trigger_scoped_to_main(self):
        # push CI must be main-only: PR-branch commits fire pull_request only (no
        # double-run / combined-status race), while merges to main still run CI for
        # the post-merge gate.
        for sid in ("python", "go", "node-ts", "rust", "generic"):
            wf = load_stacks()[sid].ci_workflow
            assert "branches: [main]" in wf, f"{sid} push not scoped to main"

    def test_default_sdlc_present(self):
        ids = set(load_sdlc().keys())
        assert {"standard", "tdd", "spec-first"} <= ids

    def test_python_stack_shape(self):
        py = load_stacks()["python"]
        assert "pytest" in py.ci_workflow
        assert "pyproject.toml" in py.scaffold
        assert py.best_practices_prompt

    def test_python_installs_deps_before_in_coder_tests(self):
        py = load_stacks()["python"]
        # install_command installs the project + deps so in-coder pytest can import them
        assert "pip install" in py.install_command
        assert "-e ." in py.install_command

    def test_python_runs_ruff_mypy_pytest_and_audit(self):
        py = load_stacks()["python"]
        for tool in ("ruff", "mypy", "pytest", "pip-audit"):
            assert tool in py.ci_workflow, f"CI workflow should run {tool}"
        # prompt nudges the coder to use the same tools
        prompt = py.best_practices_prompt.lower()
        for tool in ("ruff", "mypy", "pytest", "pip-audit"):
            assert tool in prompt, f"prompt should mention {tool}"

    def test_tdd_directive_keeps_tests_and_impl_in_one_story(self):
        tdd = load_sdlc()["tdd"]
        d = tdd.planner_directive.lower()
        # TDD is a per-story discipline; tests + impl ship together so each PR is
        # CI-green (no separate test-only story whose PR is red by design).
        assert "test-first" in d
        assert "separate" in d            # explicitly warns against splitting
        assert tdd.coder_directive.strip()


# ── catalog + fallback (Story 1.5 / 1.6) ──────────────────────────────────────

class TestCatalog:
    def test_get_stack_fallback_to_generic(self):
        c = Catalog()
        assert c.get_stack("python").id == "python"
        assert c.get_stack(None).id == "generic"
        assert c.get_stack("nonexistent").id == "generic"

    def test_get_sdlc_fallback_to_standard(self):
        c = Catalog()
        assert c.get_sdlc("tdd").id == "tdd"
        assert c.get_sdlc(None).id == "standard"
        assert c.get_sdlc("bogus").id == "standard"

    def test_list_sorted(self):
        c = Catalog()
        ids = [s.id for s in c.list_stacks()]
        assert ids == sorted(ids)

    def test_get_catalog_is_singleton(self):
        assert get_catalog() is get_catalog()

    def test_reload(self):
        c = Catalog()
        c.reload()
        assert c.has_stack("go") and c.has_sdlc("standard")


# ── catalog API (Story 1.5) ───────────────────────────────────────────────────

class TestCatalogApi:
    def test_list_stacks_endpoint(self, client):
        resp = client.get("/api/stacks")
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["stacks"]}
        assert {"python", "node-ts", "go", "generic"} <= ids
        assert all("display_name" in s and "default_sdlc" in s for s in resp.json()["stacks"])

    def test_list_sdlc_endpoint(self, client):
        resp = client.get("/api/sdlc")
        assert resp.status_code == 200
        ids = {s["id"] for s in resp.json()["sdlc"]}
        assert {"standard", "tdd", "spec-first"} <= ids

    def test_reload_endpoint(self, client):
        resp = client.post("/api/catalog/reload")
        assert resp.status_code == 200
        assert resp.json()["stacks"] >= 4 and resp.json()["sdlc"] >= 3


# ── Style guides ──────────────────────────────────────────────────────────────

class TestStyleGuides:
    def test_seed_guides_present(self):
        from event_bus.catalog import get_catalog
        ids = {g.id for g in get_catalog().list_style_guides()}
        assert {"google-python", "human-voice", "conventional-commits",
                "google-typescript", "effective-go"} <= ids

    def test_filter_by_stack(self):
        from event_bus.catalog import get_catalog
        c = get_catalog()
        py = {g.id for g in c.style_guides_for_stack("python")}
        assert "google-python" in py            # python-scoped
        assert "human-voice" in py              # cross-cutting
        assert "effective-go" not in py         # go-scoped, excluded
        # cross-cutting guides always present
        assert "conventional-commits" in {g.id for g in c.style_guides_for_stack("go")}

    def test_get_and_has(self):
        from event_bus.catalog import get_catalog
        c = get_catalog()
        assert c.has_style_guide("human-voice")
        assert not c.has_style_guide("nope")
        got = c.get_style_guides(["google-python", "nope", "human-voice"])
        assert [g.id for g in got] == ["google-python", "human-voice"]  # unknown dropped
