from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    openrouter_api_key: str = ""
    anthropic_api_key: str = ""
    model_idea: str = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"

    log_level: str = "INFO"

    @property
    def effective_api_key(self) -> str:
        return self.openrouter_api_key or self.anthropic_api_key


settings = Settings()
