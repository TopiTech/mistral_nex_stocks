"""
ai_tools.py - Mistral Native Tool Calling (Function Calling) Engine.

Provides financial tools for Mistral AI agents to autonomously retrieve:
  - Real-time stock quotes and price dynamics
  - Company fundamental financial metrics (P/E, P/B, Dividend, Market Cap)
  - Market news headlines and sentiment
  - Technical indicator levels and support/resistance boundaries
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Mistral Function Calling Tool Schemas
# ---------------------------------------------------------------------------

MISTRAL_FINANCIAL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_stock_quote",
            "description": "銘柄のリアルタイム株価、前日比、騰落率、出来高、当日高値・安値・始値を取得します。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "銘柄コードまたはシンボル (例: AAPL, 7203.T, NVDA, ^N225)",
                    },
                    "market": {
                        "type": "string",
                        "enum": ["us", "jp", "idx"],
                        "description": "市場区分 (us: 米国株, jp: 日本株, idx: 主要指数)",
                    },
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_company_fundamentals",
            "description": "企業のファンダメンタルズ財務データ（PER, PBR, 配当利回り, 時価総額, EPS, セクター, 業種等）を取得します。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "銘柄コードまたはシンボル (例: MSFT, 9984.T)",
                    },
                    "market": {
                        "type": "string",
                        "enum": ["us", "jp"],
                        "description": "市場区分 (us, jp)",
                    },
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_news",
            "description": "指定したキーワードや銘柄に関連する最新の金融・株式市況ニュースヘッドラインを取得します。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "検索キーワードまたは銘柄名/コード (例: 半導体, トヨタ, NVDA)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "取得件数 (1-10件, デフォルト5件)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_technical_levels",
            "description": "株価履歴データから主要なサポートライン・レジスタンスライン、RSI、移動平均線水準を算出します。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "銘柄コードまたはシンボル",
                    },
                    "market": {
                        "type": "string",
                        "enum": ["us", "jp"],
                        "description": "市場区分",
                    },
                    "period": {
                        "type": "string",
                        "enum": ["1mo", "3mo", "6mo", "1y"],
                        "description": "分析対象期間 (デフォルト: 3mo)",
                    },
                },
                "required": ["symbol"],
            },
        },
    },
]


def execute_mistral_tool_call(tool_name: str, arguments: dict[str, Any] | str) -> dict[str, Any]:
    """Execute a Mistral function call in a safe sandbox and return the structured result."""
    if isinstance(arguments, str):
        try:
            args = json.loads(arguments)
        except Exception:
            args = {}
    elif isinstance(arguments, dict):
        args = arguments
    else:
        args = {}

    logger.info("Executing Mistral tool call: %s with args: %s", tool_name, args)

    try:
        if tool_name == "get_stock_quote":
            return _tool_get_stock_quote(args)
        elif tool_name == "get_company_fundamentals":
            return _tool_get_company_fundamentals(args)
        elif tool_name == "get_market_news":
            return _tool_get_market_news(args)
        elif tool_name == "calculate_technical_levels":
            return _tool_calculate_technical_levels(args)
        else:
            return {"error": "未対応のツールです"}
    except Exception as exc:
        logger.warning("Tool %s execution failed: %s", tool_name, exc)
        # Tool results are supplied to the model and can be reflected in its
        # final response. Do not pass provider/implementation diagnostics into
        # that prompt; retain them only in the server log.
        return {"error": "ツールの実行に失敗しました"}


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------


def _normalize_market_symbol(args: dict[str, Any]) -> tuple[str, str]:
    raw_symbol = str(args.get("symbol", "")).strip().upper()
    market = str(args.get("market", "")).strip().lower()
    if not market:
        market = "jp" if raw_symbol.endswith(".T") or raw_symbol.isdigit() else "us"
    if market == "jp" and not raw_symbol.endswith(".T") and raw_symbol.isdigit():
        raw_symbol = f"{raw_symbol}.T"
    return raw_symbol, market


def _tool_get_stock_quote(args: dict[str, Any]) -> dict[str, Any]:
    symbol, market = _normalize_market_symbol(args)
    if not symbol:
        return {"error": "symbol is required"}

    try:
        from utils.stock_payload import get_stock_info_cached

        info = get_stock_info_cached(symbol)
        if not info or not isinstance(info, dict):
            return {"symbol": symbol, "market": market, "status": "データ取得不可または市場外"}

        return {
            "symbol": symbol,
            "name": info.get("name") or symbol,
            "market": market,
            "price": (
                info.get("price")
                or info.get("current_price")
                or info.get("regularMarketPrice")
                or info.get("currentPrice")
            ),
            "change": info.get("change") or info.get("regularMarketChange"),
            "change_pct": (
                info.get("change_pct")
                or info.get("change_percent")
                or info.get("regularMarketChangePercent")
            ),
            "volume": info.get("volume") or info.get("regularMarketVolume"),
            "high": info.get("high") or info.get("regularMarketDayHigh") or info.get("dayHigh"),
            "low": info.get("low") or info.get("regularMarketDayLow") or info.get("dayLow"),
            "open": info.get("open") or info.get("regularMarketOpen"),
            "currency": info.get("currency", "USD" if market == "us" else "JPY"),
            "updated_at": info.get("updated_at") or info.get("timestamp"),
        }
    except Exception as exc:
        logger.warning("Stock quote tool failed for %s: %s", symbol, exc)
        return {"symbol": symbol, "error": "株価情報の取得に失敗しました"}


def _tool_get_company_fundamentals(args: dict[str, Any]) -> dict[str, Any]:
    symbol, market = _normalize_market_symbol(args)
    if not symbol:
        return {"error": "symbol is required"}

    try:
        from utils.stock_payload import get_stock_info_cached

        info = get_stock_info_cached(symbol)
        if not info or not isinstance(info, dict):
            return {"symbol": symbol, "market": market, "status": "ファンダメンタルズ取得不可"}

        return {
            "symbol": symbol,
            "name": info.get("name") or symbol,
            "sector": info.get("sector", "Unknown"),
            "industry": info.get("industry", "Unknown"),
            "market_cap": info.get("market_cap") or info.get("marketCap"),
            "pe_ratio": info.get("pe_ratio") or info.get("trailing_pe") or info.get("trailingPE"),
            "forward_pe": info.get("forward_pe") or info.get("forwardPE"),
            "pb_ratio": info.get("pb_ratio") or info.get("price_to_book") or info.get("priceToBook"),
            "dividend_yield": info.get("dividend_yield") or info.get("dividendYield"),
            "eps": info.get("eps") or info.get("trailing_eps") or info.get("trailingEps"),
            "52w_high": (
                info.get("fifty_two_week_high")
                or info.get("fiftyTwoWeekHigh")
                or info.get("52w_high")
            ),
            "52w_low": (
                info.get("fifty_two_week_low")
                or info.get("fiftyTwoWeekLow")
                or info.get("52w_low")
            ),
        }
    except Exception as exc:
        logger.warning("Fundamentals tool failed for %s: %s", symbol, exc)
        return {"symbol": symbol, "error": "ファンダメンタルズ情報の取得に失敗しました"}


def _tool_get_market_news(args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query", "")).strip()
    limit = max(1, min(int(args.get("limit", 5) or 5), 10))
    if not query:
        return {"error": "query is required"}

    try:
        import trend_sources as ts

        market = "jp" if any("\u3000" <= c <= "\u9fff" for c in query) else "us"
        raw_items = ts.collect_market_news_items_fast(market)
        items: list[dict[str, Any]] = []
        for item in raw_items:
            title = getattr(item, "title", None) or (item.get("title") if isinstance(item, dict) else "")
            snippet = (
                getattr(item, "snippet", None)
                or getattr(item, "summary", None)
                or (item.get("snippet") or item.get("summary") if isinstance(item, dict) else "")
            )
            source = getattr(item, "source", None) or (item.get("source") if isinstance(item, dict) else "")
            title_str = str(title or "")
            snippet_str = str(snippet or "")
            source_str = str(source or "")
            if query.lower() in title_str.lower() or query.lower() in snippet_str.lower():
                items.append({
                    "title": title_str,
                    "snippet": snippet_str,
                    "source": source_str,
                })
            if len(items) >= limit:
                break
        return {"query": query, "count": len(items), "news": items}
    except Exception as exc:
        logger.warning("Market news tool failed for query %r: %s", query, exc)
        return {"query": query, "news": [], "error": "ニュース情報の取得に失敗しました"}


def _tool_calculate_technical_levels(args: dict[str, Any]) -> dict[str, Any]:
    symbol, _ = _normalize_market_symbol(args)
    period = str(args.get("period", "3mo")).strip() or "3mo"
    if not symbol:
        return {"error": "symbol is required"}

    try:
        from utils.market_utils import safe_get_ticker

        ticker = safe_get_ticker(symbol)
        history_df = ticker.history(period=period) if ticker is not None else None
        if history_df is None or history_df.empty or "Close" not in history_df:
            return {"symbol": symbol, "period": period, "status": "履歴データ不足"}

        closes = history_df["Close"].dropna()
        if len(closes) < 5:
            return {"symbol": symbol, "status": "データ件数不足"}

        curr_close = float(closes.iloc[-1])
        sma20 = float(closes.rolling(20).mean().iloc[-1]) if len(closes) >= 20 else None
        sma50 = float(closes.rolling(50).mean().iloc[-1]) if len(closes) >= 50 else None
        min_p = float(closes.min())
        max_p = float(closes.max())

        # Simple RSI 14
        delta = closes.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(14).mean().iloc[-1] if len(gain) >= 14 else 0.0
        avg_loss = loss.rolling(14).mean().iloc[-1] if len(loss) >= 14 else 0.0
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi14 = round(100 - (100 / (1 + rs)), 2)
        elif avg_gain > 0:
            rsi14 = 100.0
        else:
            rsi14 = 50.0

        return {
            "symbol": symbol,
            "current_price": round(curr_close, 2),
            "period": period,
            "support_level": round(min_p, 2),
            "resistance_level": round(max_p, 2),
            "sma_20": round(sma20, 2) if sma20 is not None else None,
            "sma_50": round(sma50, 2) if sma50 is not None else None,
            "rsi_14": rsi14,
            "trend_bias": "Bullish" if sma20 and curr_close > sma20 else ("Bearish" if sma20 and curr_close < sma20 else "Neutral"),
        }
    except Exception as exc:
        logger.warning("Technical levels tool failed for %s: %s", symbol, exc)
        return {"symbol": symbol, "error": "テクニカル分析用データの取得に失敗しました"}
