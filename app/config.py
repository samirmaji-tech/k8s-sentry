"""
Centralised configuration for K8s-Sentry.

All tunables come from environment variables (12-factor). In-cluster these
are injected from the `k8s-sentry-secrets` Secret; locally they come from a
`.env` file loaded by python-dotenv.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- LLM ----
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(
        default="claude-sonnet-4-5-20250929", alias="ANTHROPIC_MODEL"
    )
    llm_max_tokens: int = Field(default=1200, alias="LLM_MAX_TOKENS")

    # ---- Slack ----
    slack_webhook_url: str = Field(default="", alias="SLACK_WEBHOOK_URL")
    slack_enabled: bool = Field(default=True, alias="SLACK_ENABLED")

    # ---- Kubernetes scope ----
    target_namespace: str = Field(default="devops-lab", alias="TARGET_NAMESPACE")
    log_tail_lines: int = Field(default=50, alias="LOG_TAIL_LINES")

    # ---- Agent behaviour ----
    agent_mode: str = Field(default="read_only", alias="AGENT_MODE")
    unhealthy_reasons_raw: str = Field(
        default="CrashLoopBackOff,ImagePullBackOff,ErrImagePull,Error,"
        "CreateContainerConfigError,RunContainerError,OOMKilled",
        alias="UNHEALTHY_REASONS",
    )

    # ---- Server ----
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    @property
    def unhealthy_reasons(self) -> list[str]:
        return [r.strip() for r in self.unhealthy_reasons_raw.split(",") if r.strip()]

    @property
    def llm_enabled(self) -> bool:
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    """Cached singleton so we parse the environment exactly once."""
    return Settings()
