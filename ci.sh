#!/usr/bin/env bash
# Run all test suites with coverage. Fails if any package drops below 80%.
# Target: 90% coverage. Hard fail: 80%.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PACKAGES=(
  "event-bus"
  "agents/idea"
  "agents/planner"
  "agents/coding"
  "agents/reviewer"
)

FAILED=()

for pkg in "${PACKAGES[@]}"; do
  dir="$ROOT/$pkg"
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  $pkg"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  pushd "$dir" > /dev/null
  if ! pip install -q -e ".[dev]" 2>&1 | tail -1; then
    echo "  [FAIL] dependency install failed"
    FAILED+=("$pkg (install)")
    popd > /dev/null
    continue
  fi
  if python -m pytest "$@"; then
    echo "  [PASS] $pkg"
  else
    echo "  [FAIL] $pkg"
    FAILED+=("$pkg")
  fi
  popd > /dev/null
done

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ ${#FAILED[@]} -eq 0 ]; then
  echo "  All packages passed."
  exit 0
else
  echo "  FAILED packages:"
  for f in "${FAILED[@]}"; do
    echo "    - $f"
  done
  exit 1
fi
