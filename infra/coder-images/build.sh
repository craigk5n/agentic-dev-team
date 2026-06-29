#!/usr/bin/env bash
# Build the per-stack coder images (Story 5.1).
#
# NOT run by default — the coder works without these via the event-bus fallback:
# when a stack's coder_image is absent, _run_coding_agent_sandboxed_sync falls back
# to the default coder image (SANDBOX_IMAGE) and the coder stays language-aware via
# the stack prompts (5.3) and CI (EPIC 3). Build these only once the coding agent
# runs the stack's tests in-sandbox (true TDD red-green) and needs the toolchain.
#
# Usage:  ./build.sh [stack ...]      # default: python go node-ts
#   BASE_IMAGE=dev-agents/event-bus:latest  (override the base)
#
# Note: each image layers a language toolchain (~hundreds of MB) on the base —
# check `df -h` before building all three.
set -euo pipefail

BASE_IMAGE="${BASE_IMAGE:-dev-agents/event-bus:latest}"
cd "$(dirname "$0")"

stacks=("$@")
[ ${#stacks[@]} -eq 0 ] && stacks=(python go node-ts)

for stack in "${stacks[@]}"; do
  echo "==> Building dev-agents/coder-${stack}:latest (base: ${BASE_IMAGE})"
  docker build --build-arg BASE_IMAGE="${BASE_IMAGE}" \
    -t "dev-agents/coder-${stack}:latest" -f "Dockerfile.${stack}" .
done

echo "Built: $(docker images 'dev-agents/coder-*' --format '{{.Repository}}:{{.Tag}}' | tr '\n' ' ')"
