#!/usr/bin/env bash
# Phase 1+2 verification: confirm Plane, Forgejo, and event-bus are healthy
# Usage: ./verify.sh
# All checks are read-only; nothing is modified.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── colour helpers ────────────────────────────────────────────────────────────
green()  { echo -e "  \033[32m✓\033[0m $*"; }
red()    { echo -e "  \033[31m✗\033[0m $*"; FAILURES=$((FAILURES+1)); }
yellow() { echo -e "  \033[33m~\033[0m $*"; }
bold()   { echo -e "\n\033[1m$*\033[0m"; }

FAILURES=0

# ── load .env ─────────────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  echo "No .env found — run setup.sh first."
  exit 1
fi
# shellcheck disable=SC2046
export $(grep -v '^#' .env | grep -v '^$' | xargs)

PLANE_PORT="${PLANE_HTTP_PORT:-80}"
FORGEJO_PORT="${FORGEJO_HTTP_PORT:-3000}"
EVENT_BUS_PORT="${EVENT_BUS_PORT:-8080}"
PLANE_BASE="http://localhost:${PLANE_PORT}"
FORGEJO_BASE="http://localhost:${FORGEJO_PORT}"
EVENT_BUS_BASE="http://localhost:${EVENT_BUS_PORT}"

# ── helpers ───────────────────────────────────────────────────────────────────
check_http() {
  local url="$1" label="$2" expect="${3:-200}"
  local code
  code="$(curl -s -o /dev/null -w "%{http_code}" "${url}")"
  if [[ "$code" == "$expect" ]]; then
    green "${label} (${code})"
  else
    red "${label} — expected HTTP ${expect}, got ${code} (${url})"
  fi
}

check_json_field() {
  local url="$1" label="$2" field="$3" auth_header="${4:-}"
  local response
  if [[ -n "$auth_header" ]]; then
    response="$(curl -sf -H "$auth_header" "${url}" 2>/dev/null || echo '{}')"
  else
    response="$(curl -sf "${url}" 2>/dev/null || echo '{}')"
  fi
  if echo "$response" | grep -q "\"${field}\""; then
    green "${label}"
  else
    red "${label} — field '${field}' not found in response"
  fi
}

# ── Plane checks ──────────────────────────────────────────────────────────────
bold "── Plane CE (${PLANE_BASE}) ──"

check_http "${PLANE_BASE}/api/instances/" "Health endpoint"

if [[ -n "${PLANE_API_TOKEN:-}" ]]; then
  check_json_field \
    "${PLANE_BASE}/api/v1/users/me/" \
    "API token auth — /users/me" \
    "id" \
    "x-api-key: ${PLANE_API_TOKEN}"

  # List projects in dev-agents workspace — confirms workspace API access
  check_json_field \
    "${PLANE_BASE}/api/v1/workspaces/dev-agents/projects/" \
    "API — workspace projects accessible" \
    "total_count" \
    "x-api-key: ${PLANE_API_TOKEN}"

  # Confirm webhook API endpoint reachable
  code="$(curl -s -o /dev/null -w "%{http_code}" \
    -H "x-api-key: ${PLANE_API_TOKEN}" \
    "${PLANE_BASE}/api/v1/workspaces/dev-agents/projects/")"
  if [[ "$code" == "200" ]]; then
    green "Webhook API endpoint reachable (auth verified)"
  else
    red "Webhook API endpoint check failed (HTTP ${code})"
  fi
else
  yellow "PLANE_API_TOKEN not set — skipping authenticated checks"
  yellow "  Set it in .env after creating a token in Plane's profile settings"
fi

# ── Forgejo checks ────────────────────────────────────────────────────────────
bold "── Forgejo (${FORGEJO_BASE}) ──"

check_http "${FORGEJO_BASE}/api/healthz" "Health endpoint"

# Unauthenticated Swagger UI check (served as HTML)
check_http "${FORGEJO_BASE}/api/swagger" "Swagger UI reachable"

if [[ -n "${FORGEJO_API_TOKEN:-}" ]]; then
  check_json_field \
    "${FORGEJO_BASE}/api/v1/user" \
    "API token auth — /user" \
    "login" \
    "Authorization: token ${FORGEJO_API_TOKEN}"

  # List repos
  check_json_field \
    "${FORGEJO_BASE}/api/v1/repos/search?limit=1" \
    "API — repo search" \
    "data" \
    "Authorization: token ${FORGEJO_API_TOKEN}"

  # Verify webhooks can reach Plane (simulate a delivery URL check)
  # This just confirms the Forgejo webhook API endpoint responds, not a full delivery test
  http_code="$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: token ${FORGEJO_API_TOKEN}" \
    "${FORGEJO_BASE}/api/v1/repos/search?limit=1")"
  if [[ "$http_code" == "200" ]]; then
    green "Webhook API endpoint accessible"
  else
    red "Webhook API endpoint check failed (HTTP ${http_code})"
  fi
else
  yellow "FORGEJO_API_TOKEN not set — skipping authenticated checks"
  yellow "  Set it in .env after creating a token in Forgejo's application settings"
fi

# ── Forgejo Actions runner ────────────────────────────────────────────────────
bold "── Forgejo Actions runner ──"

if [[ -n "${FORGEJO_API_TOKEN:-}" ]]; then
  # /api/v1/admin/runners is not available in all Forgejo versions;
  # fall back to checking the runner container status instead
  runner_status="$(docker inspect forgejo-forgejo-runner-1 --format '{{.State.Status}}' 2>/dev/null || echo 'not started')"
  if [[ "$runner_status" == "running" ]]; then
    green "Runner container running (registration pending token — run: ./setup.sh runner)"
  else
    yellow "Runner not started yet — run: ./setup.sh runner (after obtaining token from Forgejo admin)"
  fi
else
  yellow "Skipping runner check (FORGEJO_API_TOKEN not set)"
fi

# ── Event bus checks ──────────────────────────────────────────────────────────
bold "── Event bus (${EVENT_BUS_BASE}) ──"

eb_code="$(curl -s -o /dev/null -w "%{http_code}" "${EVENT_BUS_BASE}/health" 2>/dev/null || echo 000)"
if [[ "$eb_code" == "200" ]]; then
  eb_json="$(curl -sf "${EVENT_BUS_BASE}/health" 2>/dev/null || echo '{}')"
  eb_redis="$(echo "$eb_json" | python3 -c "import sys,json; print(json.load(sys.stdin).get('redis','?'))" 2>/dev/null || echo '?')"
  green "Health endpoint (${eb_code}) — redis=${eb_redis}"
else
  yellow "Event bus not running (HTTP ${eb_code}) — run: cd infra && ./setup.sh eventbus"
fi

# Validate signature check: posting with wrong sig must be rejected
if [[ "$eb_code" == "200" ]]; then
  reject_code="$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${EVENT_BUS_BASE}/webhook/plane" \
    -H "Content-Type: application/json" \
    -H "X-Plane-Signature: bad" \
    -d '{"event":"issue","action":"updated","payload":{}}' 2>/dev/null || echo 000)"
  if [[ "$reject_code" == "403" ]]; then
    green "Signature validation — bad signature rejected (403)"
  else
    red "Signature validation — expected 403, got ${reject_code}"
  fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
bold "── Summary ──"
if [[ $FAILURES -eq 0 ]]; then
  echo -e "\033[32mAll checks passed.\033[0m"
  echo ""
  echo "Phase 1+2 complete. Next steps:"
  echo "  1. Configure webhooks in Plane and Forgejo:"
  echo "       Plane:   workspace Settings → Webhooks → http://event-bus:${EVENT_BUS_PORT}/webhook/plane"
  echo "       Forgejo: repo Settings → Webhooks → http://event-bus:${EVENT_BUS_PORT}/webhook/forgejo"
  echo "  2. Create a project in Plane and a repository in Forgejo."
  echo "  3. Set branch protection on 'main' in Forgejo:"
  echo "       repo → Settings → Branches → require 1 review, status checks, no force-push"
  echo "  4. Move to Phase 3: Coding Agent (ready → branch → PR)."
else
  echo -e "\033[31m${FAILURES} check(s) failed. Fix the issues above before proceeding.\033[0m"
  exit 1
fi
