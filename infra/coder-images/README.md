# Per-stack coder images (Story 5.1)

Each stack in the catalog declares a `coder_image` (e.g. `dev-agents/coder-python:latest`).
These Dockerfiles build those images = the default coder base (which already has
python3, the `coding_agent` package, and git; `opencode` is mounted at runtime) plus
the stack's language toolchain.

## Status: not built by default — intentionally deferred

The coder uses opencode (an LLM) to **write** code, then commits, pushes, and opens a
PR. It does **not** compile or run tests today — CI does (EPIC 3). So the toolchain in
these images is unused until the coding agent runs the stack's tests in-sandbox (true
TDD red-green), which is separate, larger work.

Until then the system works without them: when a stack's `coder_image` is absent,
`_run_coding_agent_sandboxed_sync` catches `ImageNotFound` and falls back to the default
coder image (`SANDBOX_IMAGE`). The coder stays language-aware via the stack prompts
(`_augment_coder_prompt`, 5.3) and the per-stack CI (EPIC 3).

## Build (when needed)

```bash
cd infra/coder-images
./build.sh                 # all three: python go node-ts
./build.sh go              # just one
BASE_IMAGE=dev-agents/event-bus:latest ./build.sh
```

Each image layers a language toolchain (~hundreds of MB) onto the base — check
`df -h` before building all three. Once built, no code change is needed: the existing
image-resolution (5.2) picks `stack.coder_image` automatically and stops falling back.
