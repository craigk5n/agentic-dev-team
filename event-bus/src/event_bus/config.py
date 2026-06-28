from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # Webhook HMAC secrets — must match what's configured in Plane/Forgejo
    plane_webhook_secret: str = ""
    forgejo_webhook_secret: str = ""

    # API tokens for outbound calls to Plane/Forgejo
    plane_api_token: str = ""
    forgejo_api_token: str = ""

    # Base URLs (internal Docker names when running in containers)
    plane_base_url: str = "http://localhost:8181"
    forgejo_base_url: str = "http://localhost:13000"
    plane_workspace_slug: str = "dev-agents"

    # Public-facing Plane URL used to build links in the UI (browser-accessible)
    plane_public_url: str = ""

    # Project where ideas and stories are created (Phase 5)
    plane_project_id: str = ""

    # Redis — use db=1 to avoid colliding with Plane's db=0
    redis_url: str = "redis://localhost:6379/1"

    # Temporal server (Phase 6) — set to enable signal-based PR approval
    temporal_address: str = ""

    # OpenRouter API key — optional; increases rate limits when fetching model list
    openrouter_api_key: str = ""

    # Fallback repo used when a story has no repo set and the planner didn't provision one
    default_repo: str = "devadmin/sandbox"

    # Max coding agents running in parallel; extras queue in ready state
    max_coding_agents: int = 2

    log_level: str = "INFO"
    port: int = 8080


settings = Settings()
