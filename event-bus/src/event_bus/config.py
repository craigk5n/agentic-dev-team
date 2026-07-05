from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # Webhook HMAC secret — must match what's configured in Forgejo
    forgejo_webhook_secret: str = ""

    # API token for outbound calls to Forgejo
    forgejo_api_token: str = ""

    # Bot usernames — added as write collaborators on provisioned repos so the
    # reviewer/coding agents can operate with their own least-privilege tokens.
    forgejo_reviewer_user: str = "reviewer-bot"
    forgejo_coder_user: str = "coder-bot"

    # Base URL (internal Docker name when running in containers)
    forgejo_base_url: str = "http://localhost:13000"

    # Board HTTP Basic Auth — single shared operator credential for the UI/API.
    # Leave board_auth_password blank to disable (the board is then fully open).
    board_auth_user: str = "admin"
    board_auth_password: str = ""

    # Redis — db=1
    redis_url: str = "redis://localhost:6379/1"

    # Temporal server (Phase 6) — set to enable signal-based PR approval
    temporal_address: str = ""

    # OpenRouter API key — optional; increases rate limits when fetching model list
    openrouter_api_key: str = ""

    # Fallback repo used when a story has no repo set and the planner didn't provision one
    default_repo: str = "devadmin/sandbox"

    # Directory where planner decompositions are frozen ("pinned") for later replay.
    # Default is repo-relative; override with PINS_DIR to a mounted volume in containers.
    pins_dir: str = "experiments/pins"

    # Max coding agents running in parallel; extras queue in ready state
    max_coding_agents: int = 2

    # Sandbox mode: "process" (default) runs coding agent in-process;
    # "docker" spawns an ephemeral container per coding run (requires socket mount)
    sandbox_mode: str = "process"
    sandbox_image: str = "dev-agents/event-bus:latest"
    sandbox_memory: str = "512m"
    sandbox_cpus: float = 1.0
    # HOST paths to bind into sandbox containers (must be host paths, not container paths)
    sandbox_opencode_bin: str = ""
    sandbox_opencode_config: str = ""
    # HOST path to the `claude` CLI — bound into the reviewer sandbox so a claude-code/*
    # (subscription) reviewer can shell out to it. Same binary the worker mounts.
    sandbox_claude_bin: str = ""
    sandbox_network: str = "forgejo_default"

    log_level: str = "INFO"
    port: int = 8080


settings = Settings()
