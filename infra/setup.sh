#!/usr/bin/env bash
# Phase 1 setup: stand up Forgejo + Forgejo Actions runner + event bus
# Usage:
#   ./setup.sh          — first-time setup (generates secrets, starts all services)
#   ./setup.sh forgejo  — start/restart Forgejo and provision accounts
#   ./setup.sh accounts — create Forgejo bot accounts + API tokens (writes .env)
#   ./setup.sh runner   — register and start the Actions runner (run after obtaining token)
#   ./setup.sh eventbus — build and start the event bus

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ── colour helpers ────────────────────────────────────────────────────────────
green()  { echo -e "\033[32m$*\033[0m"; }
yellow() { echo -e "\033[33m$*\033[0m"; }
red()    { echo -e "\033[31m$*\033[0m"; }
bold()   { echo -e "\033[1m$*\033[0m"; }

# ── prerequisites ─────────────────────────────────────────────────────────────
check_prereqs() {
  local missing=()
  command -v docker   &>/dev/null || missing+=(docker)
  command -v openssl  &>/dev/null || missing+=(openssl)
  command -v curl     &>/dev/null || missing+=(curl)

  # docker compose v2 (plugin) or v1 (standalone)
  if ! docker compose version &>/dev/null 2>&1 && ! command -v docker-compose &>/dev/null; then
    missing+=("docker-compose")
  fi

  if [[ ${#missing[@]} -gt 0 ]]; then
    red "Missing prerequisites: ${missing[*]}"
    exit 1
  fi

  # Prefer docker compose v2
  if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
  else
    DC="docker-compose"
  fi
}

# ── .env bootstrap ────────────────────────────────────────────────────────────
bootstrap_env() {
  if [[ ! -f .env ]]; then
    cp .env.example .env
    yellow "Created .env from .env.example"
  fi

  # Auto-generate any <CHANGE_ME> secret values
  local changed=false
  while IFS= read -r line; do
    if [[ "$line" == *"=<CHANGE_ME>"* ]]; then
      local key="${line%%=*}"
      local secret
      secret="$(openssl rand -hex 32)"
      # Replace only this key's value in .env
      sed -i "s|^${key}=<CHANGE_ME>|${key}=${secret}|" .env
      green "  Generated secret for ${key}"
      changed=true
    fi
  done < .env

  if [[ "$changed" == true ]]; then
    yellow "Secrets generated. Review .env before continuing."
  fi
}

# ── docker compose wrappers ───────────────────────────────────────────────────
dc_forgejo() {
  $DC -f forgejo/docker-compose.yml --env-file .env "$@"
}

dc_eventbus() {
  $DC -f event-bus/docker-compose.yml --env-file .env "$@"
}

# ── .env read/write helpers ───────────────────────────────────────────────────
env_get() { grep "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- || true; }

env_set() {
  local key="$1" val="$2"
  if grep -q "^${key}=" .env; then
    sed -i "s|^${key}=.*|${key}=${val}|" .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
}

# ── Forgejo account + token provisioning ──────────────────────────────────────
# Run the Forgejo CLI inside the container as the git user (root is rejected).
fj_cli() { dc_forgejo exec -u git -T forgejo forgejo "$@"; }

_rand_pass() { openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | cut -c1-20; }

_fj_user_exists() {
  fj_cli admin user list 2>/dev/null | awk 'NR>1{print $2}' | grep -qx "$1"
}

_fj_token_valid_for() {  # <token> <expected-username>
  local token="$1" expect="$2" port login
  port="$(env_get FORGEJO_HTTP_PORT)"; port="${port:-3000}"
  login="$(curl -s -H "Authorization: token ${token}" \
           "http://localhost:${port}/api/v1/user" 2>/dev/null \
           | grep -o '"login":"[^"]*"' | head -1 | cut -d'"' -f4 || true)"
  [[ -n "$login" && "$login" == "$expect" ]]
}

ensure_user() {  # <username> <email> <admin-flag: --admin|""> [password]
  local username="$1" email="$2" admin="$3" pass="${4:-}"
  if _fj_user_exists "$username"; then
    yellow "  user '${username}' already exists"
    return 0
  fi
  [[ -z "$pass" ]] && pass="$(_rand_pass)"
  # shellcheck disable=SC2086
  if fj_cli admin user create --username "$username" --email "$email" \
       --password "$pass" --must-change-password=false $admin >/dev/null 2>&1; then
    green "  created user '${username}'"
  else
    red "  failed to create user '${username}'"; return 1
  fi
}

ensure_token() {  # <username> <env-key> <scopes>
  local username="$1" env_key="$2" scopes="$3" existing out token name
  existing="$(env_get "$env_key")"
  if [[ -n "$existing" ]] && _fj_token_valid_for "$existing" "$username"; then
    yellow "  ${env_key} already valid for '${username}'"
    return 0
  fi
  [[ -n "$existing" ]] && yellow "  ${env_key} present but does not resolve — regenerating"
  name="agentic-$(date +%Y%m%d%H%M%S)"
  out="$(fj_cli admin user generate-access-token -u "$username" -t "$name" \
         --scopes "$scopes" 2>&1 || true)"
  token="$(echo "$out" | grep -oiE '[a-f0-9]{40}' | head -1)"
  if [[ -n "$token" ]]; then
    env_set "$env_key" "$token"
    green "  generated ${env_key} for '${username}'"
  else
    red "  token generation failed for '${username}': ${out}"; return 1
  fi
}

provision_accounts() {
  bold "Provisioning Forgejo accounts + tokens..."
  local admin_user admin_email rev_user rev_email admin_pass
  admin_user="$(env_get FORGEJO_ADMIN_USER)";    admin_user="${admin_user:-devadmin}"
  admin_email="$(env_get FORGEJO_ADMIN_EMAIL)";  admin_email="${admin_email:-${admin_user}@localhost}"
  rev_user="$(env_get FORGEJO_REVIEWER_USER)";   rev_user="${rev_user:-reviewer-bot}"
  rev_email="$(env_get FORGEJO_REVIEWER_EMAIL)"; rev_email="${rev_email:-${rev_user}@localhost}"

  # Admin operator — this is the HUMAN login for the Forgejo web UI, and also backs
  # the agent API token. Its web password lives in FORGEJO_ADMIN_PASSWORD (.env) so a
  # human can always retrieve it. Agents authenticate with tokens, not this password.
  admin_pass="$(env_get FORGEJO_ADMIN_PASSWORD)"
  if _fj_user_exists "$admin_user"; then
    if [[ -n "$admin_pass" ]]; then
      if fj_cli admin user change-password -u "$admin_user" -p "$admin_pass" \
           --must-change-password=false >/dev/null 2>&1; then
        yellow "  user '${admin_user}' exists — web password synced to FORGEJO_ADMIN_PASSWORD"
      else
        yellow "  user '${admin_user}' exists (password unchanged)"
      fi
    else
      yellow "  user '${admin_user}' exists — FORGEJO_ADMIN_PASSWORD not set; web login uses your existing password"
      yellow "    (set FORGEJO_ADMIN_PASSWORD in .env and re-run to reset it)"
    fi
  else
    if [[ -z "$admin_pass" ]]; then
      admin_pass="$(_rand_pass)"
      env_set FORGEJO_ADMIN_PASSWORD "$admin_pass"
      green "  generated FORGEJO_ADMIN_PASSWORD (saved to .env)"
    fi
    ensure_user "$admin_user" "$admin_email" "--admin" "$admin_pass"
  fi
  ensure_token "$admin_user" FORGEJO_API_TOKEN \
    "write:repository,write:user,write:issue,write:organization,write:misc,write:admin"

  # Reviewer-bot account — least-privilege identity for review/merge operations.
  # Token-only (no human login); read:user lets the bot identify itself.
  ensure_user  "$rev_user" "$rev_email" ""
  ensure_token "$rev_user" FORGEJO_REVIEWER_TOKEN \
    "read:user,write:repository,write:issue"

  # Coder-bot account — least-privilege identity for branch/commit/PR operations.
  local coder_user coder_email
  coder_user="$(env_get FORGEJO_CODER_USER)";   coder_user="${coder_user:-coder-bot}"
  coder_email="$(env_get FORGEJO_CODER_EMAIL)"; coder_email="${coder_email:-${coder_user}@localhost}"
  ensure_user  "$coder_user" "$coder_email" ""
  ensure_token "$coder_user" FORGEJO_CODER_TOKEN \
    "read:user,write:repository,write:issue"

  # Lock down self-registration now that the admin exists (effective on next forgejo restart).
  env_set FORGEJO_DISABLE_REGISTRATION true

  local port; port="$(env_get FORGEJO_HTTP_PORT)"; port="${port:-3000}"
  green "Accounts ready."
  bold  "  Human Forgejo login → http://localhost:${port}"
  echo  "    username: ${admin_user}"
  echo  "    password: value of FORGEJO_ADMIN_PASSWORD in infra/.env"
  yellow "  (FORGEJO_DISABLE_REGISTRATION=true — restart Forgejo to enforce: ./setup.sh forgejo)"
}

# ── Forgejo runner registration ───────────────────────────────────────────────
register_runner() {
  local token
  token="$(grep ^FORGEJO_RUNNER_TOKEN= .env | cut -d= -f2)"
  if [[ -z "$token" ]]; then
    yellow "FORGEJO_RUNNER_TOKEN is not set in .env"
    yellow "  1. Open Forgejo → Site Administration → Actions → Runners"
    yellow "  2. Click 'Create runner' and copy the token"
    yellow "  3. Set FORGEJO_RUNNER_TOKEN=<token> in infra/.env"
    yellow "  4. Re-run: ./setup.sh runner"
    return
  fi

  local forgejo_url
  forgejo_url="$(grep ^FORGEJO_URL= .env | cut -d= -f2)"
  forgejo_url="${forgejo_url:-http://localhost:3000}"

  bold "Registering Actions runner with Forgejo..."
  dc_forgejo exec forgejo-runner forgejo-runner register \
    --no-interactive \
    --instance "${forgejo_url}" \
    --token    "${token}" \
    --name     "local-runner" \
    --labels   "ubuntu-latest,ubuntu-22.04"

  dc_forgejo up -d forgejo-runner
  green "Runner registered and started."
}

# ── wait for service ──────────────────────────────────────────────────────────
wait_for_url() {
  local url="$1" label="$2" max="${3:-60}"
  bold "Waiting for ${label} (${url})..."
  local i=0
  until curl -sf "${url}" &>/dev/null; do
    i=$((i+1))
    if [[ $i -ge $max ]]; then
      red "${label} did not become healthy after ${max}s"
      return 1
    fi
    sleep 1
  done
  green "  ${label} is up."
}

# ── main ──────────────────────────────────────────────────────────────────────
main() {
  check_prereqs
  bootstrap_env

  local target="${1:-all}"

  case "$target" in
    forgejo)
      bold "Starting Forgejo..."
      dc_forgejo up -d forgejo-db forgejo
      local port; port="$(grep ^FORGEJO_HTTP_PORT= .env | cut -d= -f2 || echo 3000)"
      wait_for_url "http://localhost:${port}/api/healthz" "Forgejo"
      provision_accounts
      green "Forgejo is ready."
      ;;

    accounts)
      provision_accounts
      ;;

    runner)
      register_runner
      ;;

    eventbus)
      bold "Building and starting event bus..."
      dc_eventbus build
      dc_eventbus up -d
      local eb_port; eb_port="$(grep ^EVENT_BUS_PORT= .env | cut -d= -f2 || echo 8080)"
      wait_for_url "http://localhost:${eb_port}/health" "Event bus" 60
      green "Event bus ready at http://localhost:${eb_port}"
      ;;


    all)
      bold "=== Phase 1: Standing up Forgejo + runner + event bus ==="
      echo ""

      bold "── Step 1/3: Forgejo ──"
      dc_forgejo up -d forgejo-db forgejo
      local fg_port; fg_port="$(grep ^FORGEJO_HTTP_PORT= .env | cut -d= -f2 || echo 3000)"
      wait_for_url "http://localhost:${fg_port}/api/healthz" "Forgejo" 60
      green "Forgejo ready at http://localhost:${fg_port}"
      echo ""

      bold "── Step 2/3: Forgejo accounts + tokens ──"
      provision_accounts
      echo ""

      bold "── Step 3/3: Event bus ──"
      dc_eventbus build
      dc_eventbus up -d
      local eb_port; eb_port="$(grep ^EVENT_BUS_PORT= .env | cut -d= -f2 || echo 8080)"
      wait_for_url "http://localhost:${eb_port}/health" "Event bus" 60
      green "Event bus ready at http://localhost:${eb_port}"
      echo ""
      echo "  Next steps:"
      echo "  a) Register Forgejo Actions runner: ./setup.sh runner"
      echo "     (obtain token from Forgejo → Site Admin → Actions → Runners)"
      echo "  b) Configure the Forgejo webhook:"
      echo "     Forgejo: repo Settings → Webhooks → http://event-bus:${eb_port}/webhook/forgejo"
      echo "  c) Run: ./verify.sh"
      ;;

    *)
      echo "Usage: $0 [all|forgejo|accounts|runner|eventbus]"
      exit 1
      ;;
  esac
}

main "$@"
