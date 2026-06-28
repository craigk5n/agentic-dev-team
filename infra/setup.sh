#!/usr/bin/env bash
# Phase 1 setup: stand up Forgejo + Forgejo Actions runner + event bus
# Usage:
#   ./setup.sh          — first-time setup (generates secrets, starts all services)
#   ./setup.sh forgejo  — start/restart only Forgejo
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
      green "Forgejo is ready."
      yellow "Next: create the admin account at http://localhost:${port}"
      yellow "Then set FORGEJO_DISABLE_REGISTRATION=true in .env and run: ./setup.sh forgejo"
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

      bold "── Step 2/3: Manual steps needed ──"
      echo "  Forgejo: http://localhost:${fg_port} → register the admin account"
      echo "    Then:  set FORGEJO_DISABLE_REGISTRATION=true in .env"
      echo "    Then:  Profile → Settings → Applications → create token → add to FORGEJO_API_TOKEN in .env"
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
      echo "Usage: $0 [all|forgejo|runner|eventbus]"
      exit 1
      ;;
  esac
}

main "$@"
