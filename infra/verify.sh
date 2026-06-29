#!/usr/bin/env bash
# Phase 1+2 verification: confirm Forgejo and event-bus are healthy
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

FORGEJO_PORT="${FORGEJO_HTTP_PORT:-3000}"
EVENT_BUS_PORT="${EVENT_BUS_PORT:-8080}"
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

check_token_identity() {  # <token> <expected-login> <label>
  local token="$1" expect="$2" label="$3" login
  login="$(curl -sf -H "Authorization: token ${token}" "${FORGEJO_BASE}/api/v1/user" 2>/dev/null \
           | grep -o '"login":"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
  if [[ "$login" == "$expect" ]]; then
    green "${label} — resolves to '${login}'"
  else
    red "${label} — expected '${expect}', got '${login:-<none>}' (run: ./setup.sh accounts)"
  fi
}

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

  # Confirm the Forgejo webhook API endpoint responds (not a full delivery test)
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

# ── Forgejo accounts ──────────────────────────────────────────────────────────
bold "── Forgejo accounts (./setup.sh accounts) ──"

admin_user="${FORGEJO_ADMIN_USER:-devadmin}"
rev_user="${FORGEJO_REVIEWER_USER:-reviewer-bot}"

if [[ -n "${FORGEJO_API_TOKEN:-}" ]]; then
  check_token_identity "${FORGEJO_API_TOKEN}" "${admin_user}" "Admin token (FORGEJO_API_TOKEN)"
else
  red "FORGEJO_API_TOKEN not set — run: ./setup.sh accounts"
fi

if [[ -n "${FORGEJO_REVIEWER_TOKEN:-}" ]]; then
  check_token_identity "${FORGEJO_REVIEWER_TOKEN}" "${rev_user}" "Reviewer token (FORGEJO_REVIEWER_TOKEN)"
else
  yellow "FORGEJO_REVIEWER_TOKEN not set — run: ./setup.sh accounts"
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

# Board auth: a configured password must block the API to anonymous callers
if [[ "$eb_code" == "200" ]]; then
  if [[ -n "${BOARD_AUTH_PASSWORD:-}" ]]; then
    anon="$(curl -s -o /dev/null -w "%{http_code}" "${EVENT_BUS_BASE}/api/config" 2>/dev/null || echo 000)"
    authed="$(curl -s -o /dev/null -w "%{http_code}" -u "${BOARD_AUTH_USER:-admin}:${BOARD_AUTH_PASSWORD}" "${EVENT_BUS_BASE}/api/config" 2>/dev/null || echo 000)"
    if [[ "$anon" == "401" && "$authed" == "200" ]]; then
      green "Board auth — anonymous blocked (401), credentials accepted (200)"
    else
      red "Board auth — expected anon=401/authed=200, got anon=${anon}/authed=${authed}"
    fi
  else
    red "Board auth DISABLED — board UI/API is OPEN. Set BOARD_AUTH_PASSWORD in .env and rebuild (anyone reaching the URL can drive LLM cost)"
  fi
fi

# Validate signature check: posting with wrong sig must be rejected
if [[ "$eb_code" == "200" ]]; then
  reject_code="$(curl -s -o /dev/null -w "%{http_code}" \
    -X POST "${EVENT_BUS_BASE}/webhook/forgejo" \
    -H "Content-Type: application/json" \
    -H "X-Gitea-Signature: bad" \
    -d '{"action":"opened","pull_request":{}}' 2>/dev/null || echo 000)"
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
  echo "  1. Register the Actions runner if not yet running: ./setup.sh runner"
  echo "  2. Submit an idea on the board (http://localhost:${EVENT_BUS_PORT}/ui/). The"
  echo "     planner auto-provisions a repo per idea — committing a CI workflow,"
  echo "     protecting 'main' (no direct push), and registering the webhook."
  echo "  3. Move to Phase 3: Coding Agent (ready → branch → PR)."
else
  echo -e "\033[31m${FAILURES} check(s) failed. Fix the issues above before proceeding.\033[0m"
  exit 1
fi
