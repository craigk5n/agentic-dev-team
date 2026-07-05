from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # opencode model string (openrouter/provider/model or anthropic/model etc.)
    model_coder: str = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    openrouter_api_key: str = ""

    # Coder usage capture: "text" (default, proven human-format path) or "json"
    # (runs opencode with --format json to capture REAL per-turn token/cost usage).
    # Default OFF until validated against a live opencode run in the real environment.
    coder_usage_source: str = "text"

    forgejo_api_token: str = ""
    # Least-privilege coder-bot identity — used for branch/commit/PR ops so the
    # coding agent doesn't act as the admin. Falls back to forgejo_api_token if unset.
    forgejo_coder_token: str = ""
    forgejo_coder_user: str = "coder-bot"
    forgejo_base_url: str = "http://localhost:13000"
    # Internal URL used for git clone inside Docker (may differ from API base URL)
    forgejo_git_url: str = ""

    # Default target repo when story doesn't specify one (owner/name)
    default_repo: str = "devadmin/sandbox"
    # Git author identity
    git_author_name: str = "Coding Agent"
    git_author_email: str = "agent@dev-agents.local"

    log_level: str = "INFO"

    @property
    def forgejo_clone_base(self) -> str:
        return self.forgejo_git_url or self.forgejo_base_url

    @property
    def effective_forgejo_token(self) -> str:
        """Token for the coding agent's git/PR operations — prefer coder-bot."""
        return self.forgejo_coder_token or self.forgejo_api_token

    @property
    def effective_forgejo_user(self) -> str:
        """Username embedded in git auth URLs; must own the token in use."""
        return self.forgejo_coder_user if self.forgejo_coder_token else "devadmin"


settings = Settings()
