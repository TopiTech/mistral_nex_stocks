# schemas/config.py
"""Application runtime configuration and template context schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
DEFAULT_LOG_LEVEL: LogLevel = "INFO"


class SecurityConfigSchema(BaseModel):
    """Schema for app security settings."""

    production_mode: bool = Field(default=False)
    cookie_secure: bool = Field(default=False)
    csrf_enabled: bool = Field(default=True)
    rate_limit_enabled: bool = Field(default=True)
    local_only_mode: bool = Field(default=True)


class LoggingConfigSchema(BaseModel):
    """Schema for application logging settings."""

    log_level: LogLevel = Field(default=DEFAULT_LOG_LEVEL)
    json_format: bool = Field(default=True)
    log_file_enabled: bool = Field(default=True)


class RuntimeConfigSchema(BaseModel):
    """Schema for the persisted application runtime configuration."""

    port: int = Field(default=5000, ge=1024, le=65535)
    host: str = Field(default="127.0.0.1")
    simulate_fluctuation: bool = Field(default=True)
    security: SecurityConfigSchema = Field(default_factory=SecurityConfigSchema)
    logging: LoggingConfigSchema = Field(default_factory=LoggingConfigSchema)


class TemplateConfigSchema(BaseModel):
    """Schema for app config passed to templates.

    This is the only definition of the template context model.  The unrelated
    persisted-config model with the same old name is now
    ``RuntimeConfigSchema``.
    """

    has_mistral_api_key: bool
    has_openai_api_key: bool = False
    has_langsearch_api_key: bool
    has_tavily_api_key: bool
    has_alphavantage_api_key: bool
    has_ai_access: bool = False
    ai_provider: str = "mistral"
    is_beta_provider: bool = False
    provider_notice: str = ""
    openai_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-4o-mini"
    openai_embeddings_model: str = "text-embedding-3-small"
    is_local_openai_base_url: bool = False
    api_mode: str = "chat"
    agent_id: str = ""
    preview_mode: bool = False
    model_selectable: bool = True
    mistral_model: str
    is_ai_technical_lines_eligible: bool
    is_free_tier_model: bool = False
    model_tier: str = "Medium"
    mistral_api_key_min_length: int = 32
    langsearch_api_key_min_length: int = 20
    tavily_api_key_min_length: int = 5
    credentials_ephemeral: bool = False
    credentials_ephemeral_keys: list[str] = Field(default_factory=list)
    credentials_ephemeral_warning: str | None = None
