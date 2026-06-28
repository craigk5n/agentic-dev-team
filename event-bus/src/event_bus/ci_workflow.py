"""
Default CI workflow committed into every provisioned project repo.

The Forgejo Actions runner (label ubuntu-latest -> node:20-bullseye) picks this
up on push and pull_request, runs the project's tests, and reports a commit
status. That status is what the Tester verdict and branch protection can gate on.

The workflow is stack-agnostic: it detects Node (package.json) or Python
(pyproject.toml / requirements.txt / tests/) and runs the appropriate tests,
passing cleanly when there is nothing to run so an empty initial repo is green.
"""

# Path the workflow is committed to (Forgejo reads .forgejo/workflows or .gitea/workflows).
CI_WORKFLOW_PATH = ".forgejo/workflows/ci.yml"

CI_WORKFLOW_YAML = """\
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Detect stack and run tests
        run: |
          set -euo pipefail
          if [ -f package.json ]; then
            echo "::group::Node project"
            (npm ci || npm install)
            npm test --if-present
            echo "::endgroup::"
          elif [ -f pyproject.toml ] || [ -f requirements.txt ] || [ -d tests ]; then
            echo "::group::Python project"
            python3 -m ensurepip --upgrade >/dev/null 2>&1 || true
            python3 -m pip install --quiet --upgrade pip
            if [ -f requirements.txt ]; then python3 -m pip install --quiet -r requirements.txt; fi
            if [ -f pyproject.toml ]; then python3 -m pip install --quiet -e . || true; fi
            python3 -m pip install --quiet pytest
            python3 -m pytest -q
            echo "::endgroup::"
          else
            echo "No recognized test setup (no package.json / pyproject.toml / requirements.txt / tests/); nothing to run."
          fi
"""


def default_ci_workflow() -> str:
    """Return the YAML body of the default CI workflow."""
    return CI_WORKFLOW_YAML
