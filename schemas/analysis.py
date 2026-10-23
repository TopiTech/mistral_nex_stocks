# schemas/analysis.py
"""Analysis and AI chat/vision request validation schemas."""

from __future__ import annotations

import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from constants import (
    CHART_IMAGE_MAX_IMAGE_DATA_CHARS,
    CHAT_MAX_MSG_LENGTH,
    MAX_STOCK_NAME_LENGTH,
    VALID_HISTORY_PERIODS,
)
from schemas.stocks import (
    DEFAULT_STOCK_HISTORY_PERIOD,
    StockHistoryPeriod,
    StockMarket,
)
from utils.normalization import (
    is_valid_symbol,
    normalize_market,
    normalize_symbol,
    normalize_symbol_for_market,
    normalize_text,
)


class AIChatRequest(BaseModel):
    """Canonical contract for ``POST /api/chat``.

    The single rule owner for the chat body; the route validates only through
    this model (via ``route_helpers._parse_chat_request``) so the historic
    error envelope stays byte-identical. Normalization (NFKC, upper-casing,
    JP ``.T`` qualification) matches the running endpoint.
    """

    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: StockMarket = Field(default="us", description="Target stock market (us, jp, idx)")
    message: str = Field(
        ..., min_length=1, max_length=CHAT_MAX_MSG_LENGTH, description="User chat query"
    )
    request_token: str = Field(
        ..., pattern=r"^[A-Za-z0-9_-]{16,128}$", description="Client operation idempotency token"
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_identity(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw_market = data.get("market")
        if raw_market is not None and not isinstance(raw_market, str):
            raise ValueError("invalid_market")
        raw_symbol = data.get("symbol")
        if raw_symbol is not None and not isinstance(raw_symbol, str):
            raise ValueError("invalid_symbol")
        # ``None`` (absent message) normalizes to "" and fails the presence
        # check below; a non-string message carries its own marker so the
        # adapter can render the historic type message.
        raw_message: Any = data.get("message")
        if raw_message is None:
            raw_message = ""
        message: str | None = (
            raw_message.strip() if isinstance(raw_message, str) else None
        )
        if message is None:
            raise ValueError("message_must_be_string")
        market = normalize_market(raw_market, default="us")
        if not market:
            market = ""
        symbol = normalize_symbol_for_market(raw_symbol, market or "us")
        return {
            **data,
            "market": market,
            "symbol": symbol,
            "message": message,
        }

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

    @field_validator("message")
    @classmethod
    def validate_message_format(cls, v: str) -> str:
        if not v:
            raise ValueError("field_required")
        if len(v) > CHAT_MAX_MSG_LENGTH:
            raise ValueError("message_too_long")
        return v

    # ``request_token`` shape is enforced by ``pattern=`` above; the HTTP
    # adapter maps every token violation to the endpoint's historic message.


class AIAnalyzeV2Request(BaseModel):
    """Canonical contract for ``POST /api/analyze-v2``.

    The single rule owner for the analyze-v2 body; the route validates only
    through this model (via ``route_helpers._parse_analyze_v2_request``) so
    the historic error envelope stays byte-identical. Normalization (NFKC,
    upper-casing, JP ``.T`` qualification, name defaulting) matches the
    running endpoint. ``price`` keeps the client's numeric type (``int``
    stays ``int``) so the server-unreachable fallback echoes the exact JSON
    the endpoint historically echoed.
    """

    model_config = ConfigDict(allow_inf_nan=False)

    symbol: str = Field(..., min_length=1, max_length=20, description="Stock ticker symbol")
    market: StockMarket = Field(default="us", description="Target stock market (us, jp, idx)")
    name: str | None = Field(
        default=None, max_length=MAX_STOCK_NAME_LENGTH, description="Stock display name"
    )
    price: int | float | None = Field(
        default=None, ge=0.0, description="Current stock price"
    )
    chart_data: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=5000,
        description="Historical chart data points (max 5000)",
    )
    request_token: str = Field(
        ..., pattern=r"^[A-Za-z0-9_-]{16,128}$", description="Client operation idempotency token"
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_analyze_v2(cls, data: Any) -> Any:
        """Normalize route-exact inputs and raise precise failure markers.

        Mirrors the historic ``api_analyze_v2`` check order (symbol/market
        types, name type/length, chart_data shape, price shape/range, market
        presence, symbol presence/shape) and stops at the first failure, so
        the HTTP adapter replays the single error the client used to see.
        Markers surface as ``ValueError`` text inside the ``ValidationError``;
        only the adapter maps them to responses.
        """
        if not isinstance(data, dict):
            return data
        raw_symbol = data.get("symbol")
        if raw_symbol is not None and not isinstance(raw_symbol, str):
            raise ValueError("symbol_not_string")
        raw_market = data.get("market")
        if raw_market is not None and not isinstance(raw_market, str):
            raise ValueError("market_not_string")
        raw_name = data.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            raise ValueError("name_must_be_string")
        market = normalize_market(raw_market, default="us")
        symbol = normalize_symbol_for_market(raw_symbol, market)
        fallback_name = normalize_symbol(raw_symbol)
        name = normalize_text(raw_name, default=(symbol or fallback_name))
        if len(name) > MAX_STOCK_NAME_LENGTH:
            raise ValueError("name_too_long")
        raw_chart_data = data.get("chart_data", [])
        if raw_chart_data is None:
            raw_chart_data = []
        chart_list = raw_chart_data if isinstance(raw_chart_data, list) else None
        if chart_list is None:
            raise ValueError("chart_data_must_be_list")
        if len(chart_list) > 5000:
            raise ValueError("chart_data_too_many")
        for point in chart_list:
            point_dict = point if isinstance(point, dict) else None
            if point_dict is None:
                raise ValueError("chart_data_entry_not_object")
        price = data.get("price")
        if price is not None:
            if (
                isinstance(price, bool)
                or type(price).__name__ in ("bool_", "bool")
                or not isinstance(price, (int, float))
            ):
                raise ValueError("price_must_be_number")
            try:
                numeric_price = float(price)
            except OverflowError:
                raise ValueError("price_not_finite") from None
            if not math.isfinite(numeric_price):
                raise ValueError("price_not_finite")
            if numeric_price < 0.0:
                raise ValueError("price_negative")
        if not market:
            raise ValueError("market_empty")
        if not symbol:
            raise ValueError("symbol_required")
        if not is_valid_symbol(symbol):
            raise ValueError("symbol_bad_shape")
        return {
            **data,
            "market": market,
            "symbol": symbol,
            "name": name,
            "chart_data": chart_list,
            "price": price,
        }

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
    """Canonical contract for ``POST /api/ai-technical-lines``.

    The single rule owner for the technical-lines body; the route validates
    only through this model (via
    ``route_helpers._parse_technical_lines_request``) so the historic error
    envelope stays byte-identical. Normalization (market default ``us``,
    NFKC, upper-casing, JP ``.T`` qualification, period lower-casing)
    matches the running endpoint.
    """

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

    @model_validator(mode="before")
    @classmethod
    def normalize_technical_lines(cls, data: Any) -> Any:
        """Normalize route-exact inputs and raise precise failure markers.

        Mirrors the historic ``api_ai_technical_lines`` check order (symbol
        presence/type, market type/presence, symbol presence/shape, period,
        history_data shape) and stops at the first failure, so the HTTP
        adapter replays the single error the client used to see. Markers
        surface as ``ValueError`` text inside the ``ValidationError``; only
        the adapter maps them to responses.
        """
        if not isinstance(data, dict):
            return data
        raw_symbol = data.get("symbol")
        if raw_symbol is None:
            raise ValueError("symbol_required")
        symbol_text = raw_symbol if isinstance(raw_symbol, str) else None
        if symbol_text is None:
            raise ValueError("symbol_not_string")
        raw_market = data.get("market", "us")
        if raw_market is not None and not isinstance(raw_market, str):
            raise ValueError("market_not_string")
        market = normalize_market(raw_market)
        symbol = normalize_symbol_for_market(symbol_text, market)
        if not market:
            raise ValueError("market_empty")
        if not symbol:
            raise ValueError("symbol_required")
        if not is_valid_symbol(symbol):
            raise ValueError("symbol_bad_shape")
        raw_period = data.get("period", "3mo")
        period = raw_period if isinstance(raw_period, str) else None
        if period is None or period.strip().lower() not in VALID_HISTORY_PERIODS:
            raise ValueError("period_invalid")
        raw_history = data.get("history_data", [])
        if raw_history is None:
            raw_history = []
        history_list = raw_history if isinstance(raw_history, list) else None
        if history_list is None:
            raise ValueError("history_data_must_be_list")
        if len(history_list) > 5000:
            raise ValueError("history_data_too_many")
        for point in history_list:
            point_dict = point if isinstance(point, dict) else None
            if point_dict is None:
                raise ValueError("history_data_entry_not_object")
        return {
            **data,
            "market": market,
            "symbol": symbol,
            "period": period.strip().lower(),
            "history_data": history_list,
        }

    @field_validator("symbol")
    @classmethod
    def validate_symbol_format(cls, v: str) -> str:
        s = v.strip().upper()
        if not s:
            raise ValueError("Symbol cannot be empty")
        return s


class AIAnalyzeChartImageRequest(BaseModel):
    """Canonical contract for ``POST /api/analyze-chart-image``.

    The single rule owner for the chart-image body; the route validates only
    through this model (via
    ``route_helpers._parse_chart_image_request``) so the historic error
    envelope stays byte-identical. The image Data-URI/Base64 branch checks,
    symbol/market normalization, and prompt bounds match the running
    endpoint. ``image_data`` stores the merged, stripped image payload the
    old route forwarded to the vision service.
    """

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

    @model_validator(mode="before")
    @classmethod
    def normalize_chart_image(cls, data: Any) -> Any:
        """Normalize route-exact inputs and raise precise failure markers.

        Mirrors the historic ``api_analyze_chart_image`` check order (image
        presence/shape/size, Data-URI vs Base64 branch checks, symbol type/
        shape, market type/presence, prompt type/length) and stops at the
        first failure, so the HTTP adapter replays the single error the
        client used to see. Markers surface as ``ValueError`` text inside the
        ``ValidationError``; only the adapter maps them to responses.
        """
        if not isinstance(data, dict):
            return data
        raw_image = data.get("image_data") or data.get("image")
        image_text = raw_image if isinstance(raw_image, str) else None
        if image_text is None or not image_text:
            raise ValueError("image_required")
        image_text = image_text.strip()
        if not image_text:
            raise ValueError("image_required")
        if len(image_text) > CHART_IMAGE_MAX_IMAGE_DATA_CHARS:
            raise ValueError("image_too_large")
        if image_text.startswith("data:"):
            if "," not in image_text:
                raise ValueError("image_bad_data_uri")
            header, b64part = image_text.split(",", 1)
            header_lower = header.lower()
            if not header_lower.startswith("data:image/"):
                raise ValueError("image_bad_data_uri")
            if "svg" in header_lower:
                raise ValueError("image_svg")
            if not re.match(r"^data:image/(png|jpe?g|gif|webp);base64$", header_lower):
                raise ValueError("image_bad_mime")
            if not b64part.strip():
                raise ValueError("image_bad_b64")
        else:
            if image_text.startswith(("http://", "https://")):
                raise ValueError("image_url")
            if image_text.lower().startswith("data:"):
                raise ValueError("image_bad_scheme")
            if not re.fullmatch(r"[A-Za-z0-9+/=\s]+", image_text):
                raise ValueError("image_bad_base64")
        raw_symbol = data.get("symbol")
        if raw_symbol is not None and not isinstance(raw_symbol, str):
            raise ValueError("symbol_not_string")
        raw_market = data.get("market", "us")
        if raw_market is not None and not isinstance(raw_market, str):
            raise ValueError("market_not_string")
        market = normalize_market(raw_market, default="us")
        if not market:
            raise ValueError("market_empty")
        symbol = normalize_symbol_for_market(raw_symbol, market) if raw_symbol else ""
        if symbol and not is_valid_symbol(symbol):
            raise ValueError("symbol_bad_shape")
        raw_prompt = data.get("prompt", "")
        if raw_prompt is not None and not isinstance(raw_prompt, str):
            raise ValueError("prompt_must_be_string")
        custom_prompt = (raw_prompt or "").strip()
        if len(custom_prompt) > 2000:
            raise ValueError("prompt_too_long")
        return {
            **data,
            "image_data": image_text,
            "market": market,
            "symbol": symbol,
            "prompt": custom_prompt,
        }

    @model_validator(mode="after")
    def validate_image_present(self) -> AIAnalyzeChartImageRequest:
        raw = self.image_data or self.image
        if not raw or not raw.strip():
            raise ValueError("Either image_data or image must be provided")
        return self
