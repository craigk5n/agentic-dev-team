# Stack-aware planning

The system tailors each project to a **tech stack** and an **SDLC style**. When an idea
is submitted, the Idea Agent proposes a stack + style from a catalog; the operator
confirms or overrides them at approval. Those choices then drive:

- **Provisioning** — the repo gets the stack's CI workflow + scaffold files + a
  `.devagents/stack` marker, committed in one batch.
- **Planning** — the Planner decomposes per the SDLC directive (e.g. TDD ⇒ a
  "write failing tests" story before each "implement" story) and honors the stack's
  best-practices.
- **Coding & review** — the coder prompt and the reviewer prompt are augmented with the
  stack's conventions; the coder sandbox resolves the stack's image (falling back to the
  default image if it isn't built).
- **Telemetry** — LLM spend is attributed per stack (`GET /api/telemetry` → `by_stack`).

The catalog is **config-driven** — adding a stack or SDLC style needs no core-code change.

## Where definitions live

```
event-bus/src/event_bus/catalog/defaults/
├── stacks/   python.yaml  node-ts.yaml  go.yaml  rust.yaml  generic.yaml
└── sdlc/     standard.yaml  tdd.yaml  spec-first.yaml
```

Built-in defaults load first; then an optional **`CATALOG_DIR`** (mounted dir) is loaded
and **overrides by id**, so you can add or replace definitions without touching the image.
`POST /api/catalog/reload` re-reads from disk; `GET /api/stacks` and `GET /api/sdlc` list
what's loaded.

## Stack definition schema

```yaml
id: python                       # slug: ^[a-z0-9][a-z0-9-]*$  (unique)
display_name: Python             # shown in the UI
ci_image: python:3.12-slim       # reference image for the stack's CI
coder_image: dev-agents/coder-python:latest   # sandbox image for the coder (5.2)
default_sdlc: standard           # SDLC id used when none is chosen
detect:                          # hint files (future auto-detection)
  - pyproject.toml
  - "*.py"
ci_workflow: |                   # committed to .forgejo/workflows/ci.yml on provisioning
  name: CI
  on: { push: , pull_request: }
  jobs:
    test:
      runs-on: ubuntu-latest
      steps:
        - uses: actions/checkout@v4
        - run: ...               # lint + test for this stack
scaffold:                        # path -> contents, committed on fresh-repo provisioning
  pyproject.toml: |
    [project]
    name = "project"
  tests/test_smoke.py: |
    def test_smoke():
        assert True
best_practices_prompt: |         # injected into the coder + reviewer prompts
  Write idiomatic, PEP 8-compliant Python with type hints...
```

Validation rejects unknown fields, bad ids, and unsafe scaffold paths (absolute or `..`).
A malformed definition is skipped with a logged error; the rest still load.

## SDLC style schema

```yaml
id: tdd
display_name: Test-Driven Development
planner_directive: |             # shapes how the Planner splits + ORDERS stories
  Create a "write failing tests" story before each implementation story...
coder_directive: |               # appended to the coder prompt
  Follow red-green-refactor: make the new tests fail first, then implement...
story_ordering: tests-first      # informational
```

## Adding a stack

Rust ships as a built-in default (`stacks/rust.yaml` + `infra/coder-images/Dockerfile.rust`) —
use it as the reference when adding your own. The steps for a new stack, e.g. `elixir`:

1. Create `event-bus/src/event_bus/catalog/defaults/stacks/elixir.yaml` (or drop it in your
   mounted `CATALOG_DIR`). Required fields: `id`, `display_name`, `ci_image`, `coder_image`,
   `ci_workflow`; plus `scaffold`, `test_command`, `install_command`, `detect`, and
   `best_practices_prompt`. Model it on `rust.yaml` — the CI job runs inside the prebuilt
   per-stack container (`container: dev-agents/coder-elixir:latest`) so `actions/checkout`
   and the toolchain are already present.

2. Add `infra/coder-images/Dockerfile.elixir` (base `dev-agents/event-bus:latest` + the
   toolchain + node for checkout) and build it: `./infra/coder-images/build.sh elixir`.

3. Reload: `curl -u admin:$BOARD_AUTH_PASSWORD -X POST localhost:8090/api/catalog/reload`
   (or restart the event-bus). Confirm with `GET /api/stacks`.

Only one core-code touch-point exists: the in-worker tester (`agents/reviewer/test_runner.py`
`detect_test_command`) has a per-stack branch so triage runs the right command (`cargo test`,
`go test`, …) instead of the pytest fallback. Add a branch for your stack there; without it the
tester still degrades safely to a `warn` that defers to CI. Everything else — provisioning,
planning, coder prompts, approval dropdowns — is fully config-driven.

Until a coder image is built the coder falls back to the default image (CI, scaffold, and
prompts still work), but the container-based CI needs the image, so build it before real runs.

## Notes on the current CI workflows

The default stack CI workflows install the toolchain **in-step** on the runner image
(it has git + node but not pip/go). This works today but is slower than a prebuilt image;
`infra/coder-images/` holds Dockerfiles to bake per-stack toolchains when in-sandbox test
execution is added.
