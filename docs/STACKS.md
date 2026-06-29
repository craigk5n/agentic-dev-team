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
├── stacks/   python.yaml  node-ts.yaml  go.yaml  generic.yaml
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

## Worked example — add a Rust stack

1. Create `event-bus/src/event_bus/catalog/defaults/stacks/rust.yaml` (or drop it in your
   mounted `CATALOG_DIR`):

   ```yaml
   id: rust
   display_name: Rust
   ci_image: rust:1.79
   coder_image: dev-agents/coder-rust:latest
   default_sdlc: standard
   detect: ["Cargo.toml", "*.rs"]
   ci_workflow: |
     name: CI
     on: { push: , pull_request: }
     jobs:
       test:
         runs-on: ubuntu-latest
         steps:
           - uses: actions/checkout@v4
           - name: Install Rust, build, test
             run: |
               curl -sSf https://sh.rustup.rs | sh -s -- -y
               . "$HOME/.cargo/env"
               cargo fmt --check
               cargo test
   scaffold:
     Cargo.toml: |
       [package]
       name = "project"
       version = "0.1.0"
       edition = "2021"
     src/lib.rs: |
       pub fn add(a: i64, b: i64) -> i64 { a + b }

       #[cfg(test)]
       mod tests {
           use super::*;
           #[test]
           fn smoke() { assert_eq!(add(1, 1), 2); }
       }
   best_practices_prompt: |
     Write idiomatic Rust: prefer ownership over cloning, handle errors with Result
     and the ? operator, avoid unwrap() outside tests, run cargo fmt and clippy clean.
   ```

2. Reload: `curl -u admin:$BOARD_AUTH_PASSWORD -X POST localhost:8090/api/catalog/reload`
   (or restart the event-bus). Confirm with `GET /api/stacks`.

3. (Optional) Build the coder image — see `infra/coder-images/`. Until it's built, the
   coder runs in the default image; everything else (CI, scaffold, prompts) already works.

That's it — no core-code change. The new stack appears in the approval dropdowns and flows
through provisioning, planning, coding, and review.

## Notes on the current CI workflows

The default stack CI workflows install the toolchain **in-step** on the runner image
(it has git + node but not pip/go). This works today but is slower than a prebuilt image;
`infra/coder-images/` holds Dockerfiles to bake per-stack toolchains when in-sandbox test
execution is added.
