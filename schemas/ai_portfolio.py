# schemas/ai_portfolio.py
"""AI Portfolio generation, rebalancing, and persistence schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AIPortfolioItemSchema(BaseModel):
    """Schema for individual stock in AI portfolio."""

    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str = Field(..., min_length=1, max_length=20)
    name: str = Field(default="", max_length=100)
    market: Literal["us", "jp"] = Field(default="us")
    weight_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    target_price: float | None = Field(default=None, ge=0.0)
    rationale: str = Field(default="", max_length=1000)
    risk_level: Literal["low", "mid", "high"] = Field(default="mid")
    shares: float | None = Field(default=None, ge=0.0)
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    thesis: str = Field(default="", max_length=1000)
    estimated_price_jpy: float | None = Field(default=None, ge=0.0)


class AIPortfolioGenerateRequest(BaseModel):
    """Schema for /api/ai-portfolio/generate request."""

    theme: str = Field(..., min_length=1, max_length=120, description="Investment theme or focus")

    @field_validator("theme")
    @classmethod
    def validate_theme_not_blank(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Theme cannot be empty")
        return s


class AIPortfolioRebalanceRequest(BaseModel):
    """Schema for /api/ai-portfolio/rebalance request."""

    theme: str = Field(default="tech", max_length=120, description="Investment theme to rebalance")


class AIPortfolioSaveRequest(BaseModel):
    """Schema for /api/ai-portfolio/save request."""

    theme: str | None = Field(
        default=None, max_length=120, description="Portfolio theme key (optional at top-level)"
    )
    name: str | None = Field(
        default=None, max_length=120, description="Human-readable title (optional)"
    )
    portfolio: dict[str, Any] = Field(..., description="Portfolio data object")

    @field_validator("theme")
    @classmethod
    def validate_theme_not_blank(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if not s:
            raise ValueError("Theme cannot be empty")
        return s


class AIPortfolioDeleteRequest(BaseModel):
    """Schema for DELETE /api/ai-portfolio/custom request."""

    id: str = Field(..., min_length=1, max_length=256, description="Portfolio ID to delete")

    @field_validator("id")
    @classmethod
    def validate_id_not_blank(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Portfolio ID cannot be empty")
        return s


class AIPortfolioCopyToMyItem(BaseModel):
    """Schema for single stock item in /api/ai-portfolio/copy-to-my request."""

    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: Literal["us", "jp"] = Field(default="us", description="Target market")
    weight_pct: float | None = Field(default=0.0, ge=0.0, le=100.0, description="Portfolio weight percentage")
    target_price: float | None = Field(default=None, ge=0.0, description="Target price")
    shares: float | None = Field(default=None, ge=0.0, description="Stock shares")
    name: str | None = Field(default="", max_length=100, description="Stock display name")

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s


class AIPortfolioCopyToMyRequest(BaseModel):
    """Schema for POST /api/ai-portfolio/copy-to-my request body."""

    items: list[AIPortfolioCopyToMyItem] = Field(
        ..., min_length=1, max_length=20, description="List of portfolio items to copy"
    )
