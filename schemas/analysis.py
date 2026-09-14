# schemas/analysis.py
"""Analysis and AI chat/vision request validation schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from constants import (
    CHART_IMAGE_MAX_IMAGE_DATA_CHARS,
    CHAT_MAX_MSG_LENGTH,
    MAX_STOCK_NAME_LENGTH,
)
from schemas.stocks import (
    DEFAULT_STOCK_HISTORY_PERIOD,
    StockHistoryPeriod,
    StockMarket,
)


class AIChatRequest(BaseModel):
    """Schema for POST /api/chat request body."""

    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: StockMarket = Field(default="us", description="Target stock market (us, jp, idx)")
    message: str = Field(
        ..., min_length=1, max_length=CHAT_MAX_MSG_LENGTH, description="User chat query"
    )
    request_token: str = Field(
        ..., pattern=r"^[A-Za-z0-9_-]{16,128}$", description="Client operation idempotency token"
    )

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s

    @field_validator("message")
    @classmethod
    def validate_message_format(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Message cannot be empty")
        return s


class AIAnalyzeV2Request(BaseModel):
    """Schema for POST /api/analyze-v2 request body."""

    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: StockMarket = Field(default="us", description="Target stock market (us, jp, idx)")
    name: str | None = Field(
        default=None, max_length=MAX_STOCK_NAME_LENGTH, description="Stock display name"
    )
    price: float | None = Field(default=None, ge=0.0, description="Current stock price")
    chart_data: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=5000,
        description="Historical chart data points (max 5000)",
    )
    request_token: str = Field(
        ..., pattern=r"^[A-Za-z0-9_-]{16,128}$", description="Client operation idempotency token"
    )

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s


class AINewsRequest(BaseModel):
    """Schema for POST /api/news optional request body."""

    force: bool = Field(default=False, description="Force fresh fetch bypassing cached news")


class AITechnicalLinesRequest(BaseModel):
    """Schema for POST /api/ai-technical-lines request body."""

    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: StockMarket = Field(default="us", description="Target stock market (us, jp, idx)")
    period: StockHistoryPeriod = Field(
        default=DEFAULT_STOCK_HISTORY_PERIOD,
        description="Historical period for technical line analysis",
    )
    history_data: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=5000,
        description="Historical OHLCV data points (max 5000)",
    )

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s


class AIAnalyzeChartImageRequest(BaseModel):
    """Schema for POST /api/analyze-chart-image request body."""

    model_config = ConfigDict(allow_inf_nan=False)

    image_data: str | None = Field(
        default=None,
        max_length=CHART_IMAGE_MAX_IMAGE_DATA_CHARS,
        description="Base64 or Data URI encoded chart image",
    )
    image: str | None = Field(
        default=None,
        max_length=CHART_IMAGE_MAX_IMAGE_DATA_CHARS,
        description="Alias for image_data",
    )
    symbol: str | None = Field(default=None, max_length=20, description="Stock ticker symbol")
    market: StockMarket = Field(default="us", description="Target stock market")
    prompt: str = Field(default="", max_length=2000, description="Custom analysis prompt")

    @model_validator(mode="after")
    def validate_image_present(self) -> AIAnalyzeChartImageRequest:
        raw = self.image_data or self.image
        if not raw or not raw.strip():
            raise ValueError("Either image_data or image must be provided")
        return self
