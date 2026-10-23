# schemas/ai_portfolio.py
"""AI Portfolio generation, rebalancing, and persistence schemas."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from constants import (
    AI_PORTFOLIO_MARKETS,
    MAX_AI_PORTFOLIO_ITEMS,
    MAX_STOCK_NAME_LENGTH,
    PORTFOLIO_AVG_PRICE_MAX,
)
from utils.normalization import (
    is_valid_symbol,
    market_symbol_mismatch,
    normalize_symbol_for_market,
)


class AIPortfolioItemSchema(BaseModel):
    """Schema for individual stock in AI portfolio."""

    model_config = ConfigDict(allow_inf_nan=False, str_strip_whitespace=True)

    symbol: str = Field(..., min_length=1, max_length=20)
    name: str = Field(default="", max_length=MAX_STOCK_NAME_LENGTH)
    market: Literal["us", "jp"] = Field(default="us")
    weight_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    target_price: float | None = Field(default=None, ge=0.0, le=PORTFOLIO_AVG_PRICE_MAX)
    rationale: str = Field(default="", max_length=1000)
    risk_level: Literal["low", "mid", "high"] = Field(default="mid")
    shares: float | None = Field(default=None, ge=0.0)
    weight: float | None = Field(default=None, ge=0.0, le=1.0)
    thesis: str = Field(default="", max_length=1000)
    estimated_price_jpy: float | None = Field(default=None, ge=0.0)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        symbol = value.strip().upper()
        if not is_valid_symbol(symbol):
            raise ValueError("invalid stock symbol")
        return symbol

    @field_validator("market", "risk_level", mode="before")
    @classmethod
    def normalize_choice(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator(
        "weight_pct",
        "target_price",
        "shares",
        "weight",
        "estimated_price_jpy",
        mode="before",
    )
    @classmethod
    def reject_boolean_numeric(cls, v: Any) -> Any:
        """Reject boolean values for numeric fields."""
        if isinstance(v, bool) or type(v).__name__ in ("bool_", "bool"):
            raise ValueError("bool_type_not_allowed")
        return v


def _is_bool_like(value: Any) -> bool:
    """Return True for bool and numpy-style bool scalars (route-exact check)."""
    return isinstance(value, bool) or type(value).__name__ in ("bool_", "bool")


class AIPortfolioGenerateRequest(BaseModel):
    """Canonical contract for ``POST /api/ai-portfolio/generate``.

    The single rule owner for the generate body; the route validates only
    through this model (via ``route_helpers._parse_ai_generate_request``).
    """

    theme: str = Field(..., min_length=1, max_length=120, description="Investment theme or focus")

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = data.get("theme")
        if raw is None:
            return data
        theme: str | None = raw.strip() if isinstance(raw, str) else None
        if theme is None:
            raise ValueError("theme_must_be_string")
        return {**data, "theme": theme}

    @field_validator("theme")
    @classmethod
    def validate_theme_not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("field_required")
        return v


class AIPortfolioRebalanceRequest(BaseModel):
    """Canonical contract for ``POST /api/ai-portfolio/rebalance``.

    An absent theme falls back to ``"tech"``; a blank or over-long theme is
    a malformed request, matching the running endpoint.
    """

    theme: str = Field(default="tech", max_length=120, description="Investment theme to rebalance")

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = data.get("theme")
        if raw is None:
            return data
        theme: str | None = raw.strip() if isinstance(raw, str) else None
        if theme is None:
            raise ValueError("theme_must_be_string")
        return {**data, "theme": theme}


class AIPortfolioSaveRequest(BaseModel):
    """Canonical contract for ``POST /api/ai-portfolio/save``.

    Every body failure maps to the endpoint's single malformed envelope, so
    the pre-check duplicate in the route is gone; this model is the owner.
    """

    theme: str | None = Field(
        default=None, max_length=120, description="Portfolio theme key (optional at top-level)"
    )
    name: str | None = Field(
        default=None, max_length=120, description="Human-readable title (optional)"
    )
    portfolio: dict[str, Any] = Field(..., description="Portfolio data object")

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = data.get("portfolio")
        portfolio: dict[str, Any] | None = raw if isinstance(raw, dict) else None
        if portfolio is None:
            raise ValueError("invalid_portfolio")
        return data

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
    """Canonical contract for ``DELETE /api/ai-portfolio/custom``."""

    id: str = Field(..., min_length=1, max_length=256, description="Portfolio ID to delete")

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = data.get("id")
        if raw is None:
            return data
        candidate: str | None = raw.strip() if isinstance(raw, str) else None
        if candidate is None:
            raise ValueError("id_must_be_string")
        return {**data, "id": candidate}

    @field_validator("id")
    @classmethod
    def validate_id_not_blank(cls, v: str) -> str:
        if not v:
            raise ValueError("field_required")
        return v


class AIPortfolioCopyToMyItem(BaseModel):
    """Schema for single stock item in /api/ai-portfolio/copy-to-my request."""

    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: Literal["us", "jp"] = Field(default="us", description="Target market")
    weight_pct: float | None = Field(
        default=None, gt=0.0, le=100.0, description="Portfolio weight percentage"
    )
    target_price: float | None = Field(
        default=None, gt=0.0, le=PORTFOLIO_AVG_PRICE_MAX, description="Target price"
    )
    shares: float | None = Field(default=None, ge=0.0, description="Stock shares")
    name: str | None = Field(
        default="", max_length=MAX_STOCK_NAME_LENGTH, description="Stock display name"
    )

    @field_validator("weight_pct", "target_price", "shares", mode="before")
    @classmethod
    def reject_boolean_numeric(cls, v: Any) -> Any:
        """Reject boolean values for numeric fields."""
        if isinstance(v, bool) or type(v).__name__ in ("bool_", "bool"):
            raise ValueError("bool_type_not_allowed")
        return v

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s


class AIPortfolioCopyToMyRequest(BaseModel):
    """Canonical contract for ``POST /api/ai-portfolio/copy-to-my``.

    The single rule owner for the copy body. The ``mode="before"`` validator
    below replays the endpoint's historic sequential checks verbatim for raw
    JSON dicts (market coercion, JP ``.T`` qualification, per-item index
    errors, duplicate and total-weight gates) and normalizes items to
    ``{symbol, market, target_price, weight_pct}``; route-ignored free
    fields (``name``, ``shares``) never cause rejection, exactly as the
    running endpoint. Markers (``copy_*``) let ``route_helpers`` render the
    byte-identical envelopes.

    Programmatic construction from ``AIPortfolioCopyToMyItem`` objects keeps
    the legacy field/after validation untouched (it never reaches the HTTP
    route, which only receives JSON dicts), so directly-constructed
    consumers see the exact historical messages.
    """

    items: list[AIPortfolioCopyToMyItem] = Field(
        ...,
        min_length=1,
        max_length=MAX_AI_PORTFOLIO_ITEMS,
        description="List of portfolio items to copy",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_and_validate_items(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_items = data.get("items")
        if isinstance(raw_items, list) and raw_items and all(
            isinstance(entry, AIPortfolioCopyToMyItem) for entry in raw_items
        ):
            # Programmatic path: leave legacy field/after validation in charge.
            return data
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("copy_items_required")
        if len(raw_items) > MAX_AI_PORTFOLIO_ITEMS:
            raise ValueError("copy_too_many")
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        total_weight = 0.0
        for idx, raw in enumerate(raw_items):
            if isinstance(raw, AIPortfolioCopyToMyItem):
                raw = raw.model_dump()
            item_dict: dict[str, Any] | None = raw if isinstance(raw, dict) else None
            if item_dict is None:
                raise ValueError(f"copy_item_not_object|{idx}")
            raw = item_dict
            market = str(raw.get("market") or "us").strip().lower()
            symbol = normalize_symbol_for_market(raw.get("symbol"), market)
            if market not in AI_PORTFOLIO_MARKETS or not is_valid_symbol(symbol):
                raise ValueError(f"copy_bad_symbol_market|{idx}")
            mismatch = market_symbol_mismatch(symbol, market)
            if mismatch:
                raise ValueError(f"copy_mismatch|{idx}|{mismatch}")
            if (symbol, market) in seen:
                # Keep the historical direct-construction wording (pinned by
                # tests) and append the index for the route envelope mapper.
                raise ValueError(
                    f"Duplicate stock in items: {symbol} ({market}) (item {idx})"
                )
            seen.add((symbol, market))
            target_raw = raw.get("target_price")
            weight_raw = raw.get("weight_pct")
            if _is_bool_like(target_raw) or _is_bool_like(weight_raw):
                raise ValueError(f"copy_bool_numeric|{idx}")
            try:
                if target_raw is None or (
                    isinstance(target_raw, str) and not target_raw.strip()
                ):
                    target_price = 100.0
                else:
                    target_price = float(target_raw)
                weight_pct = float(weight_raw or 0.0)
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"copy_bad_numeric|{idx}") from None
            if (
                not math.isfinite(target_price)
                or target_price <= 0
                or target_price > PORTFOLIO_AVG_PRICE_MAX
            ):
                raise ValueError(f"copy_bad_target_range|{idx}")
            if not 0.0 < weight_pct <= 100.0 or not math.isfinite(weight_pct):
                raise ValueError(f"copy_bad_weight_range|{idx}")
            total_weight += weight_pct
            normalized.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "target_price": target_price,
                    "weight_pct": weight_pct,
                }
            )
        if total_weight > 100.5:
            raise ValueError(
                f"Total weight_pct exceeds 100% (got {total_weight:.2f}%)"
            )
        return {**data, "items": normalized}

    @model_validator(mode="after")
    def validate_unique_symbols_and_total_weight(self) -> AIPortfolioCopyToMyRequest:
        # Defense-in-depth: reachable only for data that passed the sequential
        # gate above, so this is a no-op for route traffic and a guard for
        # future edits. Messages stay compatible with direct construction.
        seen: set[tuple[str, str]] = set()
        total_weight: float = 0.0
        for item in self.items:
            norm_symbol = normalize_symbol_for_market(item.symbol, item.market)
            key = (norm_symbol, item.market)
            if key in seen:
                raise ValueError(f"Duplicate stock in items: {key[0]} ({key[1]})")
            seen.add(key)
            if item.weight_pct is not None:
                total_weight += item.weight_pct
        if total_weight > 100.5:
            raise ValueError(
                f"Total weight_pct exceeds 100% (got {total_weight:.2f}%)"
            )
        return self


class AIPortfolioResponseSchema(BaseModel):
    """Schema for complete AI Portfolio response."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=500)
    risk_level: str = Field(default="中リスク", min_length=1, max_length=500)
    expected_return: str = Field(default="8-12%", min_length=1, max_length=500)
    commentary: str = Field(default="", max_length=500)
    items: list[AIPortfolioItemSchema] = Field(min_length=1, max_length=20)


# Aliases for backward compatibility and uniform imports across modules
AiPortfolioItemSchema = AIPortfolioItemSchema
AiPortfolioResponseSchema = AIPortfolioResponseSchema
