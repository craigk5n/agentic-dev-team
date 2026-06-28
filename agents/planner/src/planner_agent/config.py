from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    openrouter_api_key: str = ""
    anthropic_api_key: str = ""
    model_planner: str = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"

    # Default repo included in story descriptions so the Coding Agent knows where to work
    default_repo: str = "devadmin/sandbox"

    log_level: str = "INFO"

    @property
    def effective_api_key(self) -> str:
        return self.openrouter_api_key or self.anthropic_api_key


settings = Settings()
