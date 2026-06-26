#!/usr/bin/env bash
# protect.sh — configure Forgejo branch protection so agents cannot push directly to main.
#
# Run this once after setup.sh has created the initial repos.
# It protects the main branch of DEFAULT_REPO (default: devadmin/sandbox) and
# optionally creates a dedicated 'agent' Forgejo user with limited access.
#
# Usage:
#   cd infra
#   source .env   # or ensure env vars are set
#   ./protect.sh [owner/repo ...]
#
# Multiple repos can be passed as arguments.  If none are given, DEFAULT_REPO is used.

set -euo pipefail

FORGEJO_URL="${FORGEJO_BASE_URL:-http://localhost:${FORGEJO_HTTP_PORT:-13000}}"
ADMIN_TOKEN="${FORGEJO_API_TOKEN:?FORGEJO_API_TOKEN must be set}"
REPOS=("${@:-${DEFAULT_REPO:-devadmin/sandbox}}")

# ── Helper ───────────────────────────────────────────────────────────────────

forgejo_api() {
    local method="$1" path="$2"
    shift 2
    curl -sSf -X "$method" \
        "${FORGEJO_URL}/api/v1${path}" \
        -H "Authorization: token ${ADMIN_TOKEN}" \
        -H "Content-Type: application/json" \
        "$@"
}

# ── Protect main branch in each repo ─────────────────────────────────────────

for repo in "${REPOS[@]}"; do
    owner="${repo%%/*}"
    name="${repo##*/}"
    echo "→ Protecting main branch in ${owner}/${name} …"

    # Check if protection already exists
    existing=$(forgejo_api GET "/repos/${owner}/${name}/branches/main/protection" 2>/dev/null || echo "")
    if echo "$existing" | grep -q '"branch_name"'; then
        echo "  Already protected — updating."
        METHOD="PATCH"
    else
        METHOD="POST"
    fi

    forgejo_api "$METHOD" "/repos/${owner}/${name}/branches/main/protection" -d '{
      "branch_name": "main",
      "enable_push": true,
      "enable_push_whitelist": true,
      "push_whitelist_usernames": [],
      "push_whitelist_teams": [],
      "push_whitelist_deploy_keys": false,
      "enable_merge_whitelist": false,
      "required_approvals": 0,
      "enable_approvals_whitelist": false,
      "require_signed_commits": false,
      "protected_file_patterns": "",
      "unprotected_file_patterns": "",
      "block_on_rejected_reviews": false,
      "block_on_official_review_requests": false,
      "block_on_outdated_branch": false
    }' > /dev/null
    echo "  ✓ main branch in ${owner}/${name} is protected."
done

# ── Verify ───────────────────────────────────────────────────────────────────

echo ""
echo "Branch protection summary:"
for repo in "${REPOS[@]}"; do
    owner="${repo%%/*}"
    name="${repo##*/}"
    result=$(forgejo_api GET "/repos/${owner}/${name}/branches/main/protection" 2>/dev/null || echo '{}')
    enabled=$(echo "$result" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('branch_name') else 'no')" 2>/dev/null || echo "unknown")
    echo "  ${owner}/${name}/main — protected: ${enabled}"
done

echo ""
echo "done. Agents can push to feature branches but not to main."
echo "Set SANDBOX_MODE=docker in .env to run each agent in an ephemeral container"
echo "with least-privilege credentials (see event-bus/src/event_bus/sandbox.py)."
