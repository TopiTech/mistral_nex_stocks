# schemas/stocks.py
"""Stock request and query validation schemas."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from constants import (
    MAX_STOCK_NAME_LENGTH,
    PORTFOLIO_AVG_FX_RATE_MAX,
    PORTFOLIO_AVG_PRICE_MAX,
    PORTFOLIO_SHARES_MAX,
    PORTFOLIO_TOTAL_VALUE_MAX,
)
from utils.normalization import (
    is_valid_symbol,
    normalize_market,
    normalize_symbol,
    normalize_symbol_for_market,
    normalize_text,
)

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


def _normalize_stock_mutation_identity(data: Any, default_market: str) -> Any:
    """Apply the historic ingress normalization for watchlist mutations.

    Mirrors ``route_helpers._parse_stock_request``: non-string ``market`` /
    ``symbol`` / ``name`` are surfaced as typed markers so the HTTP adapter
    can render the exact historic error envelope; normalization itself
    (NFKC, upper-casing, JP ``.T`` qualification) is identical.
    """
    if not isinstance(data, dict):
        return data
    raw_market_val = data.get("market")
    if raw_market_val is not None and not isinstance(raw_market_val, str):
        raise ValueError("invalid_market")
    raw_symbol_val = data.get("symbol")
    if raw_symbol_val is not None and not isinstance(raw_symbol_val, str):
        raise ValueError("invalid_symbol")
    raw_name_val = data.get("name")
    if raw_name_val is not None and not isinstance(raw_name_val, str):
        raise ValueError("name_must_be_string")
    raw_symbol = normalize_symbol(raw_symbol_val)
    market = normalize_market(raw_market_val, default=default_market)
    if not market:
        market = ""
    symbol = normalize_symbol_for_market(raw_symbol, market) if market else ""
    name = normalize_text(raw_name_val)
    return {**data, "market": market, "symbol": symbol, "name": name}


class StockAddRequest(BaseModel):
    """Canonical contract for ``POST /api/stocks/add``.

    The single rule owner for the add body; the route validates only through
    this model (via ``route_helpers._parse_stock_add_request``) so the
    historic error envelope stays byte-identical.
    """

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    name: str = Field(..., description="Stock display name")
    market: StockMarket = Field(..., description="Target market")

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, data: Any) -> Any:
        return _normalize_stock_mutation_identity(data, default_market="")

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        if not v:
            raise ValueError("field_required")
        if not is_valid_symbol(v):
            raise ValueError("invalid_symbol")
        return v

    @field_validator("market")
    @classmethod
    def validate_market_present(cls, v: str) -> str:
        if not v:
            raise ValueError("invalid_market")
        return v

    @field_validator("name")
    @classmethod
    def validate_name_format(cls, v: str) -> str:
        if not v:
            raise ValueError("field_required_name")
        if len(v) > MAX_STOCK_NAME_LENGTH:
            raise ValueError("name_too_long")
        return v


class StockAddExtRequest(BaseModel):
    """Canonical contract for ``POST /api/stocks/add_ext``.

    ``market`` defaults to ``us`` and ``name`` is optional, matching the
    running extension endpoint; validation is otherwise identical to add.
    """

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    name: str | None = Field(
        default=None,
        description="Stock display name (optional)",
    )
    market: StockMarket = Field(default="us", description="Target market")

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, data: Any) -> Any:
        normalized = _normalize_stock_mutation_identity(data, default_market="us")
        if isinstance(normalized, dict) and normalized.get("name") == "":
            raw_name = data.get("name") if isinstance(data, dict) else None
            if raw_name is None:
                normalized = {**normalized, "name": None}
        return normalized

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        if not v:
            raise ValueError("field_required")
        if not is_valid_symbol(v):
            raise ValueError("invalid_symbol")
        return v

    @field_validator("market")
    @classmethod
    def validate_market_present(cls, v: str) -> str:
        if not v:
            raise ValueError("invalid_market")
        return v

    @field_validator("name")
    @classmethod
    def validate_name_format(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        if len(v) > MAX_STOCK_NAME_LENGTH:
            raise ValueError("name_too_long")
        return v


class StockDeleteRequest(BaseModel):
    """Canonical contract for ``POST|DELETE /api/stocks/delete``."""

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: StockMarket = Field(..., description="Target market")

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, data: Any) -> Any:
        return _normalize_stock_mutation_identity(data, default_market="")

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        if not v:
            raise ValueError("field_required")
        if not is_valid_symbol(v):
            raise ValueError("invalid_symbol")
        return v

    @field_validator("market")
    @classmethod
    def validate_market_present(cls, v: str) -> str:
        if not v:
            raise ValueError("invalid_market")
        return v


class PortfolioUpdateRequest(BaseModel):
    """Canonical contract for the ``POST /api/stocks/portfolio`` body.

    The endpoint used to validate these fields in three places (this model,
    ``utils.validators.PortfolioInputSchema`` and the route's own
    ``parse_non_negative_float`` calls) with slightly different rules.  Those
    duplicates are gone; the HTTP route now validates only through this model.

    Intent resolution (the duplicates disagreed here):

    * ``avg_fx_rate`` is accepted for every market.  The running endpoint keeps
      a non-US ``avg_fx_rate`` out of storage instead of rejecting the request
      with a 400 (see ``api_update_portfolio``); that production behavior wins.
    * ``shares``/``avg_price`` accept numeric strings and reject booleans, NaN
      and infinity, matching ``parse_non_negative_float``.
    """

    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: StockMarket = Field(..., description="Target market")
    shares: float = Field(..., ge=0.0, le=PORTFOLIO_SHARES_MAX, description="Number of shares held")
    avg_price: float = Field(..., ge=0.0, le=PORTFOLIO_AVG_PRICE_MAX, description="Average purchase price")
    avg_fx_rate: float | None = Field(
        default=None, gt=0.0, le=PORTFOLIO_AVG_FX_RATE_MAX, description="Average USD/JPY FX rate"
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, data: Any) -> Any:
        """Apply the same symbol/market normalization the route always used."""
        if not isinstance(data, dict):
            return data
        raw_market = data.get("market")
        if raw_market is not None and not isinstance(raw_market, str):
            raise ValueError("invalid_market")
        raw_symbol = data.get("symbol")
        if raw_symbol is not None and not isinstance(raw_symbol, str):
            raise ValueError("invalid_symbol")
        market = normalize_market(raw_market, default="")
        symbol = normalize_symbol_for_market(normalize_symbol(raw_symbol), market) if market else ""
        return {**data, "market": market, "symbol": symbol}

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        if not v:
            raise ValueError("field_required")
        if not is_valid_symbol(v):
            raise ValueError("invalid_symbol")
        return v

    @field_validator("shares", "avg_price", mode="before")
    @classmethod
    def require_numeric_fields(cls, v: Any) -> Any:
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("field_required")
        if isinstance(v, bool) or type(v).__name__ in ("bool_", "bool"):
            raise ValueError("bool_type_not_allowed")
        return v

    @field_validator("avg_fx_rate", mode="before")
    @classmethod
    def normalize_optional_fx_rate(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        if isinstance(v, bool) or type(v).__name__ in ("bool_", "bool"):
            raise ValueError("bool_type_not_allowed")
        return v

    @model_validator(mode="after")
    def validate_total_value(self) -> PortfolioUpdateRequest:
        if self.shares * self.avg_price > PORTFOLIO_TOTAL_VALUE_MAX:
            raise ValueError(
                f"Portfolio total value exceeds maximum allowed ({PORTFOLIO_TOTAL_VALUE_MAX:,})"
            )
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
    max_price: float | None = Field(default=None, ge=0.0, description="Maximum price filter")
    min_change: float | None = Field(
        default=None, ge=-100.0, le=10000.0, description="Minimum change percentage"
    )
    max_change: float | None = Field(
        default=None, ge=-100.0, le=10000.0, description="Maximum change percentage"
    )
    min_market_cap: float | None = Field(
        default=None, ge=0.0, description="Minimum market cap filter"
    )
    max_market_cap: float | None = Field(
        default=None, ge=0.0, description="Maximum market cap filter"
    )
    min_pe: float | None = Field(default=None, ge=0.0, description="Minimum P/E ratio filter")
    max_pe: float | None = Field(default=None, ge=0.0, description="Maximum P/E ratio filter")
    limit: int = Field(default=150, ge=1, le=500, description="Maximum items to return")

    @field_validator("market", mode="before")
    @classmethod
    def _normalize_screener_market(cls, v: Any) -> Any:
        if v is None or v == "":
            return "all"
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("sector", mode="before")
    @classmethod
    def _normalize_screener_sector(cls, v: Any) -> Any:
        if v is None or v == "":
            return "all"
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("q", mode="before")
    @classmethod
    def _normalize_screener_q(cls, v: Any) -> Any:
        if v is None:
            return ""
        if isinstance(v, str):
            return v.strip()
        return v

    @field_validator("sort_by", mode="before")
    @classmethod
    def _normalize_screener_sort_by(cls, v: Any) -> Any:
        if v is None or v == "":
            return DEFAULT_SCREENER_SORT_BY
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("sort_order", mode="before")
    @classmethod
    def _normalize_screener_sort_order(cls, v: Any) -> Any:
        if v is None or v == "":
            return "desc"
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("limit", mode="before")
    @classmethod
    def _normalize_screener_limit(cls, v: Any) -> Any:
        if v is None or v == "":
            return 150
        if isinstance(v, str):
            try:
                return int(v.strip())
            except (ValueError, TypeError) as exc:
                raise ValueError("limit_must_be_integer") from exc
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        raise ValueError("limit_must_be_integer")

    @field_validator(
        "min_price",
        "max_price",
        "min_change",
        "max_change",
        "min_market_cap",
        "max_market_cap",
        "min_pe",
        "max_pe",
        mode="before",
    )
    @classmethod
    def reject_boolean_numeric(cls, v: Any, info: ValidationInfo) -> Any:
        """Reject boolean values and parse numeric string inputs for filter fields."""
        if isinstance(v, bool) or type(v).__name__ in ("bool_", "bool"):
            raise ValueError("bool_type_not_allowed")
        if v is None:
            return None
        if isinstance(v, str):
            v_str = v.strip()
            if not v_str:
                return None
            try:
                val = float(v_str)
            except (ValueError, TypeError) as exc:
                raise ValueError("must_be_number") from exc
            if not math.isfinite(val):
                raise ValueError("must_be_finite")
            field_name = info.field_name or ""
            if field_name not in ("min_change", "max_change") and val < 0:
                raise ValueError("must_be_non_negative")
            return val
        if isinstance(v, (int, float)):
            if not math.isfinite(v):
                raise ValueError("must_be_finite")
            field_name = info.field_name or ""
            if field_name not in ("min_change", "max_change") and v < 0:
                raise ValueError("must_be_non_negative")
            return float(v)
        raise ValueError("must_be_number")

    @model_validator(mode="after")
    def validate_bounds(self) -> ScreenerQueryRequest:
        if (
            self.min_price is not None
            and self.max_price is not None
            and self.min_price > self.max_price
        ):
            raise ValueError("min_price_greater_than_max")
        if (
            self.min_change is not None
            and self.max_change is not None
            and self.min_change > self.max_change
        ):
            raise ValueError("min_change_greater_than_max")
        if (
            self.min_market_cap is not None
            and self.max_market_cap is not None
            and self.min_market_cap > self.max_market_cap
        ):
            raise ValueError("min_market_cap_greater_than_max")
        if (
            self.min_pe is not None
            and self.max_pe is not None
            and self.min_pe > self.max_pe
        ):
            raise ValueError("min_pe_greater_than_max")
        return self


class StockHistoryQueryRequest(BaseModel):
    """Schema for /api/stock-history query parameters."""

    model_config = ConfigDict(str_strip_whitespace=True)

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

    @field_validator("market", mode="before")
    @classmethod
    def _normalize_market_val(cls, v: Any) -> Any:
        if v is None or v == "":
            return "us"
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("period", mode="before")
    @classmethod
    def _normalize_period_val(cls, v: Any) -> Any:
        if v is None or v == "":
            return DEFAULT_STOCK_HISTORY_PERIOD
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("interval", mode="before")
    @classmethod
    def _normalize_interval_val(cls, v: Any) -> Any:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol_format(cls, v: Any) -> Any:
        if isinstance(v, str):
            norm = normalize_symbol(v)
            if not norm:
                raise ValueError("Symbol cannot be empty")
            return norm
        if not v:
            raise ValueError("Symbol cannot be empty")
        return v

    @model_validator(mode="after")
    def _validate_symbol_and_market(self) -> StockHistoryQueryRequest:
        norm_sym = normalize_symbol_for_market(self.symbol, self.market)
        if not is_valid_symbol(norm_sym):
            raise ValueError("Invalid symbol format")
        self.symbol = norm_sym
        return self


class StockDetailsQueryRequest(BaseModel):
    """Schema for /api/stock-details query parameters."""

    model_config = ConfigDict(str_strip_whitespace=True)

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: Literal["us", "jp", "idx"] = Field(default="us", description="Target market")

    @field_validator("market", mode="before")
    @classmethod
    def _normalize_market_val(cls, v: Any) -> Any:
        if v is None or v == "":
            return "us"
        if isinstance(v, str):
            return v.strip().lower()
        return v

    @field_validator("symbol", mode="before")
    @classmethod
    def validate_symbol_format(cls, v: Any) -> Any:
        if isinstance(v, str):
            norm = normalize_symbol(v)
            if not norm:
                raise ValueError("Symbol cannot be empty")
            return norm
        if not v:
            raise ValueError("Symbol cannot be empty")
        return v

    @model_validator(mode="after")
    def _validate_symbol_and_market(self) -> StockDetailsQueryRequest:
        norm_sym = normalize_symbol_for_market(self.symbol, self.market)
        if not is_valid_symbol(norm_sym):
            raise ValueError("Invalid symbol format")
        self.symbol = norm_sym
        return self


class HeatmapQueryRequest(BaseModel):
    """Schema for /api/heatmap query parameters."""

    model_config = ConfigDict(str_strip_whitespace=True)

    market: Literal["us", "jp"] = Field(default="us", description="Target market")

    @field_validator("market", mode="before")
    @classmethod
    def _normalize_market_val(cls, v: Any) -> Any:
        if v is None or v == "":
            return "us"
        if isinstance(v, str):
            norm = normalize_market(v, default="us")
            if not norm:
                raise ValueError("invalid_market")
            if norm not in ("us", "jp"):
                raise ValueError("unsupported_heatmap_market")
            return norm
        return v
