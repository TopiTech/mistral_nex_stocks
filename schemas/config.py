# schemas/config.py
"""App configuration and security settings schemas."""

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


class AppConfigSchema(BaseModel):
    """Schema for global application configuration."""

    port: int = Field(default=5000, ge=1024, le=65535)
    host: str = Field(default="127.0.0.1")
    simulate_fluctuation: bool = Field(default=True)
    security: SecurityConfigSchema = Field(default_factory=SecurityConfigSchema)
    logging: LoggingConfigSchema = Field(default_factory=LoggingConfigSchema)
