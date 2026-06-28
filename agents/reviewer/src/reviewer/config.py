from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    # LLM credentials — set openrouter_api_key to route everything through OpenRouter,
    # or anthropic_api_key for direct Anthropic calls.
    openrouter_api_key: str = ""
    anthropic_api_key: str = ""

    # Per-role model selection (litellm model strings).
    # OpenRouter format:  openrouter/{provider}/{model}
    # Anthropic direct:   anthropic/{model}
    # Ollama local:       ollama/{model}
    model_reviewer: str = "openrouter/nvidia/nemotron-3-super-120b-a12b:free"
    model_tester: str = "openrouter/meta-llama/llama-3.3-70b-instruct:free"
    model_security: str = "openrouter/meta-llama/llama-3.3-70b-instruct:free"

    # Forgejo
    forgejo_api_token: str = ""
    forgejo_reviewer_token: str = ""  # reviewer-bot user — may differ from coder token
    forgejo_base_url: str = "http://localhost:3000"
    forgejo_git_url: str = ""

    # Event-bus internal URL — used to trigger recode without going through Forgejo webhooks
    event_bus_internal_url: str = "http://event-bus:8080"

    # Redis — same db as event-bus worker
    redis_url: str = "redis://localhost:6379/1"
    # How long verdicts live in Redis (seconds)
    verdict_ttl: int = 3600

    # CI wait gate — before auto-merging, wait for the Forgejo Actions CI status on
    # the PR head to resolve. A red CI triggers a recode; a hang holds the merge.
    # Repos with no CI workflow report no status, so after ci_wait_grace seconds with
    # zero statuses we treat CI as absent and proceed (don't block uninstrumented repos).
    ci_wait_enabled: bool = True
    ci_wait_timeout: int = 600   # max seconds to wait for CI to finish
    ci_wait_interval: int = 5    # seconds between status polls
    ci_wait_grace: int = 45      # seconds to wait for the first status before assuming no CI

    log_level: str = "INFO"

    @property
    def effective_api_key(self) -> str:
        return self.openrouter_api_key or self.anthropic_api_key

    @property
    def forgejo_clone_base(self) -> str:
        return self.forgejo_git_url or self.forgejo_base_url


settings = Settings()
