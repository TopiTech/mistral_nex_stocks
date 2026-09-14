# schemas/__init__.py
"""Pydantic validation schemas for API requests, responses, and configurations."""

from __future__ import annotations

from schemas.ai_portfolio import (
    AIPortfolioCopyToMyItem,
    AIPortfolioCopyToMyRequest,
    AIPortfolioDeleteRequest,
    AIPortfolioGenerateRequest,
    AIPortfolioItemSchema,
    AIPortfolioRebalanceRequest,
    AIPortfolioSaveRequest,
)
from schemas.analysis import (
    AIAnalyzeChartImageRequest,
    AIAnalyzeV2Request,
    AIChatRequest,
    AINewsRequest,
    AITechnicalLinesRequest,
)
from schemas.config import (
    AppConfigSchema,
    LoggingConfigSchema,
    SecurityConfigSchema,
)
from schemas.stocks import (
    HeatmapQueryRequest,
    PortfolioUpdateRequest,
    ScreenerQueryRequest,
    StockAddExtRequest,
    StockAddRequest,
    StockDeleteRequest,
    StockDetailsQueryRequest,
    StockHistoryQueryRequest,
)

__all__ = [
    "AIAnalyzeChartImageRequest",
    "AIAnalyzeV2Request",
    "AIChatRequest",
    "AINewsRequest",
    "AIPortfolioCopyToMyItem",
    "AIPortfolioCopyToMyRequest",
    "AIPortfolioDeleteRequest",
    "AIPortfolioGenerateRequest",
    "AIPortfolioItemSchema",
    "AIPortfolioRebalanceRequest",
    "AIPortfolioSaveRequest",
    "AITechnicalLinesRequest",
    "AppConfigSchema",
    "HeatmapQueryRequest",
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
