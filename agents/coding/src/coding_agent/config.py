from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # opencode model string (openrouter/provider/model or anthropic/model etc.)
    model_coder: str = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    openrouter_api_key: str = ""

    forgejo_api_token: str = ""
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


settings = Settings()
