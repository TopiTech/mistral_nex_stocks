# schemas/stocks.py
"""Stock request and query validation schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
StockMarket = Literal["us", "jp", "idx"]


class StockAddRequest(BaseModel):
    """Schema for /api/stocks/add request body."""

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    name: str = Field(..., min_length=1, max_length=100, description="Stock display name")
    market: StockMarket = Field(..., description="Target market")

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
    market: StockMarket = Field(default="us", description="Target market")

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
    market: StockMarket = Field(..., description="Target market")

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s


class PortfolioUpdateRequest(BaseModel):
    """Schema for /api/stocks/portfolio request body."""

    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: StockMarket = Field(..., description="Target market")
    shares: float = Field(..., ge=0.0, le=1_000_000_000.0, description="Number of shares held")
    avg_price: float = Field(..., ge=0.0, le=1_000_000_000.0, description="Average purchase price")
    avg_fx_rate: float | None = Field(
        default=None, gt=0.0, le=1_000_000.0, description="Average USD/JPY FX rate (US market only)"
    )

    @model_validator(mode="after")
    def validate_market_fx_rate(self) -> PortfolioUpdateRequest:
        if self.market != "us" and self.avg_fx_rate is not None:
            raise ValueError("avg_fx_rate is only supported for the US market")
        return self


class ScreenerQueryRequest(BaseModel):
    """Schema for /api/screener query parameters."""

    model_config = ConfigDict(allow_inf_nan=False)

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

    @model_validator(mode="after")
    def validate_bounds(self) -> ScreenerQueryRequest:
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price cannot be greater than max_price")
        if (
            self.min_change is not None
            and self.max_change is not None
            and self.min_change > self.max_change
        ):
            raise ValueError("min_change cannot be greater than max_change")
        if (
            self.min_market_cap is not None
            and self.max_market_cap is not None
            and self.min_market_cap > self.max_market_cap
        ):
            raise ValueError("min_market_cap cannot be greater than max_market_cap")
        if (
            self.min_pe is not None
            and self.max_pe is not None
            and self.min_pe > self.max_pe
        ):
            raise ValueError("min_pe cannot be greater than max_pe")
        return self


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

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s


class StockDetailsQueryRequest(BaseModel):
    """Schema for /api/stock-details query parameters."""

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: Literal["us", "jp", "idx"] = Field(default="us", description="Target market")

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s

