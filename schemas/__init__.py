# schemas/__init__.py
"""Pydantic validation schemas for API requests, responses, and configurations."""

from __future__ import annotations

from schemas.ai_portfolio import (
    AIPortfolioGenerateRequest,
    AIPortfolioItemSchema,
    AIPortfolioRebalanceRequest,
    AIPortfolioSaveRequest,
)
from schemas.config import (
    AppConfigSchema,
    LoggingConfigSchema,
    SecurityConfigSchema,
)
from schemas.stocks import (
    PortfolioUpdateRequest,
    ScreenerQueryRequest,
    StockAddExtRequest,
    StockAddRequest,
    StockDeleteRequest,
    StockDetailsQueryRequest,
    StockHistoryQueryRequest,
)

__all__ = [
    "AIPortfolioGenerateRequest",
    "AIPortfolioItemSchema",
    "AIPortfolioRebalanceRequest",
    "AIPortfolioSaveRequest",
    "AppConfigSchema",
    "LoggingConfigSchema",
    "PortfolioUpdateRequest",
    "ScreenerQueryRequest",
    "SecurityConfigSchema",
    "StockAddExtRequest",
    "StockAddRequest",
    "StockDeleteRequest",
    "StockDetailsQueryRequest",
    "StockHistoryQueryRequest",
]
