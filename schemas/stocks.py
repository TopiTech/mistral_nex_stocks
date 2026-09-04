# schemas/stocks.py
"""Stock request and query validation schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ScreenerSortBy = Literal[
    "market_cap",
    "price",
    "change_percent",
    "change_pct",
    "volume",
    "symbol",
    "pe_ratio",
    "pe",
]
DEFAULT_SCREENER_SORT_BY: ScreenerSortBy = "market_cap"

StockHistoryPeriod = Literal["1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "max"]
DEFAULT_STOCK_HISTORY_PERIOD: StockHistoryPeriod = "3mo"


class StockAddRequest(BaseModel):
    """Schema for /api/stocks/add request body."""

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    name: str = Field(..., min_length=1, max_length=100, description="Stock display name")
    market: Literal["us", "jp"] = Field(..., description="Target market")

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s

    @field_validator("name")
    @classmethod
    def validate_name_format(cls, v: str) -> str:
        s = v.strip()
        if not s:
            raise ValueError("Name cannot be empty")
        return s


class StockAddExtRequest(BaseModel):
    """Schema for /api/stocks/add_ext request body."""

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    name: str | None = Field(
        default=None, max_length=100, description="Stock display name (optional)"
    )
    market: Literal["us", "jp"] = Field(default="us", description="Target market")

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s


class StockDeleteRequest(BaseModel):
    """Schema for /api/stocks/delete request body."""

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: Literal["us", "jp"] = Field(..., description="Target market")

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s


class PortfolioUpdateRequest(BaseModel):
    """Schema for /api/stocks/portfolio request body."""

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: Literal["us", "jp"] = Field(..., description="Target market")
    shares: float = Field(..., ge=0.0, le=1_000_000_000.0, description="Number of shares held")
    avg_price: float = Field(..., ge=0.0, le=1_000_000_000.0, description="Average purchase price")
    avg_fx_rate: float | None = Field(
        default=None, ge=0.0, le=1_000_000.0, description="Average USD/JPY FX rate (US market only)"
    )


class ScreenerQueryRequest(BaseModel):
    """Schema for /api/screener query parameters."""

    market: Literal["all", "us", "jp"] = Field(default="all", description="Market filter")
    sector: str = Field(default="all", max_length=100, description="Sector filter")
    q: str = Field(default="", max_length=200, description="Search query")
    sort_by: ScreenerSortBy = Field(
        default=DEFAULT_SCREENER_SORT_BY,
        description="Sort field",
    )
    sort_order: Literal["asc", "desc"] = Field(default="desc", description="Sort direction")
    min_price: float | None = Field(default=None, ge=0.0, description="Minimum price filter")
    max_price: float | None = Field(default=None, gt=0.0, description="Maximum price filter")
    min_change: float | None = Field(default=None, description="Minimum change percentage")
    max_change: float | None = Field(default=None, description="Maximum change percentage")
    min_market_cap: float | None = Field(
        default=None, ge=0.0, description="Minimum market cap filter"
    )
    max_market_cap: float | None = Field(
        default=None, gt=0.0, description="Maximum market cap filter"
    )
    min_pe: float | None = Field(default=None, ge=0.0, description="Minimum P/E ratio filter")
    max_pe: float | None = Field(default=None, gt=0.0, description="Maximum P/E ratio filter")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum items to return")


class StockHistoryQueryRequest(BaseModel):
    """Schema for /api/stock-history query parameters."""

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: Literal["us", "jp", "idx"] = Field(default="us", description="Target market")
    period: StockHistoryPeriod = Field(
        default=DEFAULT_STOCK_HISTORY_PERIOD,
        description="Historical data period",
    )
    interval: (
        Literal["auto", "1m", "2m", "5m", "15m", "30m", "60m", "1h", "1d", "5d", "1wk", "1mo"]
        | None
    ) = Field(default=None, description="Data interval")


class StockDetailsQueryRequest(BaseModel):
    """Schema for /api/stock-details query parameters."""

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: Literal["us", "jp", "idx"] = Field(default="us", description="Target market")
