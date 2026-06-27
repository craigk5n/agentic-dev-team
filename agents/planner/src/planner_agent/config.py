from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    openrouter_api_key: str = ""
    anthropic_api_key: str = ""
    model_planner: str = "openrouter/anthropic/claude-sonnet-4-6"

    plane_api_token: str = ""
    plane_base_url: str = "http://localhost:8181"
    plane_workspace_slug: str = "dev-agents"

    # Default repo included in story descriptions so the Coding Agent knows where to work
    default_repo: str = "devadmin/sandbox"

    log_level: str = "INFO"

    @property
    def effective_api_key(self) -> str:
        return self.openrouter_api_key or self.anthropic_api_key


settings = Settings()
