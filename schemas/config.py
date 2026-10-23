# schemas/config.py
"""Application runtime configuration and template context schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from constants import (
    AGENT_ID_RE,
    LANGSEARCH_API_KEY_MIN_LENGTH,
    MISTRAL_API_KEY_MIN_LENGTH,
    TAVILY_API_KEY_MIN_LENGTH,
)
from utils.text_utils import _is_valid_api_key

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


class CredentialsSaveRequest(BaseModel):
    """Canonical contract for ``POST /api/credentials``.

    The single rule owner for the credentials-save body; the route validates
    only field shapes through this model (via
    ``route_helpers._parse_credentials_save_request``) so the historic error
    envelope stays byte-identical. Server-state-dependent rules (Agents-mode
    gating, model catalog resolution) stay in the route. Key format checks
    use the shared ``_is_valid_api_key`` predicate with the same per-field
    minimum lengths the old route used.
    """

    model_config = ConfigDict(extra="ignore")

    mistral_api_key: str | None = Field(default=None)
    openai_api_key: str | None = Field(default=None)
    langsearch_api_key: str | None = Field(default=None)
    tavily_api_key: str | None = Field(default=None)
    alphavantage_api_key: str | None = Field(default=None)
    custom_ai_prompt: str | None = Field(default=None)
    mistral_api_mode: str | None = Field(default=None)
    mistral_agent_id: str | None = Field(default=None)
    preview_mode: bool | None = Field(default=None)
    mistral_model: str | None = Field(default=None)
    ai_provider: str | None = Field(default=None)
    openai_base_url: str | None = Field(default=None)
    openai_model: str | None = Field(default=None)
    openai_embeddings_model: str | None = Field(default=None)

    @model_validator(mode="before")
    @classmethod
    def normalize_credentials_save(cls, data: Any) -> Any:
        """Normalize route-exact inputs and raise precise failure markers.

        Mirrors the historic ``api_credentials`` POST check order (key
        types, key formats, prompt, mode, agent id, preview flag, model
        shape, provider, base URL, model names) and stops at the first
        failure, so the HTTP adapter replays the single error the client
        used to see. Markers surface as ``ValueError`` text inside the
        ``ValidationError``; only the adapter maps them to responses.
        """
        if not isinstance(data, dict):
            return data
        stripped: dict[str, str | None] = {}
        for field_name in (
            "mistral_api_key",
            "openai_api_key",
            "langsearch_api_key",
            "tavily_api_key",
            "alphavantage_api_key",
        ):
            raw_value = data.get(field_name)
            value_text = raw_value if isinstance(raw_value, str) else None
            if raw_value is not None and value_text is None:
                raise ValueError(f"cred_not_string|{field_name}")
            stripped[field_name] = value_text.strip() if value_text is not None else None
        key_lengths = (
            ("mistral_api_key", MISTRAL_API_KEY_MIN_LENGTH),
            ("openai_api_key", 8),
            ("langsearch_api_key", LANGSEARCH_API_KEY_MIN_LENGTH),
            ("tavily_api_key", TAVILY_API_KEY_MIN_LENGTH),
            ("alphavantage_api_key", 10),
        )
        for field_name, min_length in key_lengths:
            candidate = stripped[field_name]
            if candidate and not _is_valid_api_key(candidate, min_length=min_length):
                raise ValueError(f"cred_bad_key|{field_name}")
        prompt_present = "custom_ai_prompt" in data
        raw_prompt = data.get("custom_ai_prompt")
        prompt_text = raw_prompt if isinstance(raw_prompt, str) else None
        if prompt_present and raw_prompt is not None and prompt_text is None:
            raise ValueError("prompt_not_string")
        prompt_value = (prompt_text or "").strip() if prompt_present else None
        if prompt_value is not None and len(prompt_value) > 5000:
            raise ValueError("prompt_too_long")
        raw_mode = data.get("mistral_api_mode")
        if raw_mode is not None and raw_mode not in ("chat", "agents"):
            raise ValueError("mode_invalid")
        raw_agent_id = data.get("mistral_agent_id")
        agent_text = raw_agent_id if isinstance(raw_agent_id, str) else None
        if raw_agent_id is not None and agent_text is None:
            raise ValueError("agent_not_string")
        target_agent_id = agent_text.strip() if agent_text is not None else None
        if target_agent_id and not AGENT_ID_RE.fullmatch(target_agent_id):
            raise ValueError("agent_bad_format")
        raw_preview = data.get("preview_mode")
        if raw_preview is not None and not isinstance(raw_preview, bool):
            raise ValueError("preview_not_bool")
        raw_model = data.get("mistral_model")
        model_text = raw_model if isinstance(raw_model, str) else None
        if raw_model is not None and (model_text is None or not model_text.strip()):
            raise ValueError("model_not_string")
        raw_provider = data.get("ai_provider")
        if raw_provider is not None and raw_provider not in ("mistral", "openai_compatible"):
            raise ValueError("provider_invalid")
        raw_url = data.get("openai_base_url")
        url_text = raw_url if isinstance(raw_url, str) else None
        if raw_url is not None and url_text is None:
            raise ValueError("url_not_string")
        target_url = url_text.strip().rstrip("/") if url_text is not None else None
        if target_url and not target_url.startswith(("http://", "https://")):
            raise ValueError("url_bad_scheme")
        raw_openai_model = data.get("openai_model")
        openai_model_text = raw_openai_model if isinstance(raw_openai_model, str) else None
        if raw_openai_model is not None and openai_model_text is None:
            raise ValueError("openai_model_not_string")
        raw_emb_model = data.get("openai_embeddings_model")
        emb_model_text = raw_emb_model if isinstance(raw_emb_model, str) else None
        if raw_emb_model is not None and emb_model_text is None:
            raise ValueError("openai_emb_model_not_string")
        return {
            **data,
            **stripped,
            "custom_ai_prompt": prompt_value,
            "mistral_api_mode": raw_mode,
            "mistral_agent_id": target_agent_id,
            "preview_mode": raw_preview,
            "mistral_model": model_text.strip() if model_text is not None else None,
            "ai_provider": raw_provider,
            "openai_base_url": target_url,
            "openai_model": openai_model_text.strip()
            if openai_model_text is not None
            else None,
            "openai_embeddings_model": emb_model_text.strip()
            if emb_model_text is not None
            else None,
        }


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
