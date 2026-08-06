"""utils/tradingview_mapper.py - TradingView symbol mapping utility.

Converts internal stock tickers (e.g., 7203.T, AAPL, ^GSPC, ^N225) into official
TradingView exchange-prefixed symbol identifiers (e.g., TSE:7203, NASDAQ:AAPL, FOREXCOM:SPXUSD).
"""

import threading

# Known market index and watchlist mappings to TradingView proNames.
# NOTE: mirrored in static/js/tradingview_manager.js mapTickerToTvSymbol as the
# client-side fallback for non-SSE paths (/api/stocks, mode 0/1). Keep both in sync.
INDEX_MAP: dict[str, dict[str, str]] = {
    "^GSPC": {"proName": "FOREXCOM:SPXUSD", "title": "S&P 500", "description": "S&P 500"},
    "SPX": {"proName": "FOREXCOM:SPXUSD", "title": "S&P 500", "description": "S&P 500"},
    "^IXIC": {"proName": "FOREXCOM:NSXUSD", "title": "ナスダック総合", "description": "ナスダック総合"},
    "NASDAQ": {"proName": "FOREXCOM:NSXUSD", "title": "ナスダック総合", "description": "ナスダック総合"},
    "^DJI": {"proName": "FOREXCOM:DJI", "title": "Dow Jones", "description": "Dow Jones"},
    "DJI": {"proName": "FOREXCOM:DJI", "title": "ダウ平均", "description": "ダウ平均"},
    "^N225": {"proName": "INDEX:NKY", "title": "日経225", "description": "日経225"},
    "NI225": {"proName": "INDEX:NKY", "title": "日経225", "description": "日経225"},
    "^TOPX": {"proName": "TSE:TOPIX", "title": "TOPIX", "description": "TOPIX"},
    "TOPIX": {"proName": "TSE:TOPIX", "title": "TOPIX", "description": "TOPIX"},
    "DXY": {"proName": "CAPITALCOM:DXY", "title": "ドルインデックス", "description": "ドルインデックス"},
    "^VIX": {"proName": "CAPITALCOM:VIX", "title": "VIX指数", "description": "VIX指数"},
    "VIX": {"proName": "CAPITALCOM:VIX", "title": "VIX指数", "description": "VIX指数"},
    "US10Y": {"proName": "FRED:DGS10", "title": "米10年債", "description": "米10年債"},
    "GOLD": {"proName": "TVC:GOLD", "title": "金", "description": "金"},
    "USOIL": {"proName": "TVC:USOIL", "title": "原油", "description": "原油"},
    "NK2251!": {"proName": "FOREXCOM:JP225", "title": "日経225先物", "description": "日経225先物"},
    "USDJPY": {"proName": "FX:USDJPY", "title": "ドル円", "description": "ドル円"},
    "EURUSD": {"proName": "FX:EURUSD", "title": "ユーロドル", "description": "ユーロドル"},
    "GBPJPY": {"proName": "FX:GBPJPY", "title": "ポンド円", "description": "ポンド円"},
    "BTCUSD": {"proName": "BITSTAMP:BTCUSD", "title": "BTC/USD", "description": "BTC/USD"},
    "BTCJPY": {"proName": "BITFLYER:BTCJPY", "title": "BTC/JPY", "description": "BTC/JPY"},
    "BTCUSD.P": {"proName": "COINBASE:BTCUSD", "title": "BTCUSD.P", "description": "BTCUSD.P"},
}

# Header keys used by _resolve_indices_for_response() -> internal index symbols.
# Lets the ticker tape reflect the indices actually present in the live payload.
_INDEX_HEADER_TO_SYMBOL: dict[str, str] = {
    "N225": "^N225",
    "DJI": "^DJI",
    "SP500": "^GSPC",
    "NASDAQ": "^IXIC",
    "VIX": "^VIX",
    "TOPX": "^TOPX",
    "US10Y": "US10Y",
    "USDJPY": "USDJPY=X",
    "EURJPY": "EURJPY=X",
}

# Known US stock exchange overrides (manual fallback overrides)
US_STOCK_EXCHANGE_MAP: dict[str, str] = {
    # Custom manual overrides can be added here if needed
}

# Major JP stock overrides if special handling is needed
JP_STOCK_EXCHANGE_MAP: dict[str, str] = {
    # Default is TSE for Japanese tickers ending in .T
}


# Thread-safe in-memory cache for dynamic ticker -> exchange mapping
_TICKER_EXCHANGE_CACHE: dict[str, str] = {
    # Popular/common NYSE stocks as pre-populated safety cache (dynamic updates will enrich this)
    "IBM": "NYSE",
    "IONQ": "NYSE",
    "BRK.A": "NYSE",
    "BRK.B": "NYSE",
    "JNJ": "NYSE",
    "JPM": "NYSE",
    "V": "NYSE",
    "MA": "NYSE",
    "UNH": "NYSE",
    "PG": "NYSE",
    "HD": "NYSE",
    "BAC": "NYSE",
    "XOM": "NYSE",
    "CVX": "NYSE",
    "KO": "NYSE",
    "PEP": "NASDAQ",
    "NKE": "NYSE",
    "DIS": "NYSE",
    "WMT": "NYSE",
    "LLY": "NYSE",
    "ORCL": "NYSE",
    "PLTR": "NYSE",
    "PFE": "NYSE",
    "ABBV": "NYSE",
    "MRK": "NYSE",
    "CRM": "NYSE",
    "BABA": "NYSE",
    "SONY": "NYSE",
    "MUFG": "NYSE",
    "SMFG": "NYSE",
    "TM": "NYSE",
    "HMC": "NYSE",
}

_CACHE_LOCK = threading.Lock()


def resolve_exchange_prefix(exchange: str | None) -> str | None:
    """Resolve Yahoo Finance exchange code/name to TradingView exchange prefix.

    Examples:
        - "NYQ" / "NYSE" / "NYS" -> "NYSE"
        - "NMS" / "NGM" / "NCM" / "NASDAQ" -> "NASDAQ"
        - "ASE" / "AMEX" -> "AMEX"
        - "TSE" / "TYO" / "JPX" -> "TSE"
    """
    if not exchange or not isinstance(exchange, str):
        return None
    ex = exchange.strip().upper()

    if ex in ("NYQ", "NYSE", "NYS", "NEW YORK STOCK EXCHANGE", "ARC", "ARCA", "NYSE ARCA", "NYSE MKT"):
        return "NYSE"
    if ex in (
        "NMS",
        "NGM",
        "NCM",
        "NASDAQ",
        "NASDAQGS",
        "NASDAQGM",
        "NASDAQCM",
        "NASDAQ STOCK MARKET",
    ):
        return "NASDAQ"
    if ex in ("ASE", "AMEX", "NYSE AMERICAN"):
        return "AMEX"
    if ex in ("TSE", "TYO", "JPX", "TOKYO"):
        return "TSE"

    if "NYSE" in ex or "NYQ" in ex or "ARCA" in ex:
        return "NYSE"
    if "NASDAQ" in ex or "NMS" in ex or "NGM" in ex or "NCM" in ex:
        return "NASDAQ"
    if "AMEX" in ex or "AMERICAN" in ex:
        return "AMEX"

    return None


def register_ticker_exchange(ticker: str, exchange: str | None) -> None:
    """Register or cache a resolved exchange prefix for a given ticker."""
    if not ticker:
        return
    prefix = resolve_exchange_prefix(exchange)
    if prefix:
        clean_ticker = ticker.strip().upper()
        with _CACHE_LOCK:
            _TICKER_EXCHANGE_CACHE[clean_ticker] = prefix


def _resolve_ticker_exchange_dynamically(ticker: str) -> str | None:
    """Attempt dynamic lookup of exchange code for a given ticker."""
    clean_ticker = ticker.strip().upper()

    # 1. Check in-memory cache
    with _CACHE_LOCK:
        if clean_ticker in _TICKER_EXCHANGE_CACHE:
            return _TICKER_EXCHANGE_CACHE[clean_ticker]

    # 2. Check stock payload info cache
    try:
        from utils.stock_payload import get_stock_info_cached

        cached_info = get_stock_info_cached(clean_ticker, cache_only=True)
        if isinstance(cached_info, dict) and cached_info.get("exchange"):
            prefix = resolve_exchange_prefix(cached_info.get("exchange"))
            if prefix:
                register_ticker_exchange(clean_ticker, prefix)
                return prefix
    except Exception:
        pass

    # 3. Dynamic lookup via yfinance fast_info if not in cache
    try:
        import yfinance as yf

        t = yf.Ticker(clean_ticker)
        ex = None
        if hasattr(t, "fast_info") and t.fast_info:
            try:
                ex = t.fast_info.get("exchange") if hasattr(t.fast_info, "get") else getattr(t.fast_info, "exchange", None)
            except Exception:
                ex = None
        if not ex and hasattr(t, "info"):
            try:
                info = t.info
                if isinstance(info, dict):
                    ex = info.get("exchange")
            except Exception:
                ex = None

        prefix = resolve_exchange_prefix(ex)
        if prefix:
            register_ticker_exchange(clean_ticker, prefix)
            return prefix
    except Exception:
        pass

    return None


def get_tradingview_symbol(ticker: str, exchange: str | None = None) -> str:
    """Convert an internal stock ticker or index symbol to a TradingView symbol.

    Examples:
        - "7203.T" -> "TSE:7203"
        - "AAPL" (exchange="NMS") -> "NASDAQ:AAPL"
        - "IONQ" (exchange="NYQ") -> "NYSE:IONQ"
        - "IBM" -> "NYSE:IBM"
        - "^GSPC" -> "FOREXCOM:SPXUSD"
        - "^N225" -> "INDEX:NKY"
    """
    if not ticker:
        return ""

    ticker_clean = ticker.strip().upper()

    # 1. Check Index Map
    if ticker_clean in INDEX_MAP:
        return INDEX_MAP[ticker_clean]["proName"]

    # 2. Check Japanese Stock (.T suffix)
    if ticker_clean.endswith(".T"):
        code = ticker_clean[:-2]
        return f"TSE:{code}"

    # 3. Check US Exchange manual overrides
    if ticker_clean in US_STOCK_EXCHANGE_MAP:
        return f"{US_STOCK_EXCHANGE_MAP[ticker_clean]}:{ticker_clean}"

    # 4. Known US Index symbols
    if ticker_clean.startswith("^"):
        symbol_name = ticker_clean[1:]
        return f"INDEX:{symbol_name}"

    # 5. Dynamic exchange lookup
    prefix = resolve_exchange_prefix(exchange)
    if prefix:
        register_ticker_exchange(ticker_clean, prefix)
        return f"{prefix}:{ticker_clean}"

    # 6. Resolve exchange dynamically via cache/yfinance if exchange was not provided
    dynamic_prefix = _resolve_ticker_exchange_dynamically(ticker_clean)
    if dynamic_prefix:
        return f"{dynamic_prefix}:{ticker_clean}"

    # Standard US stock symbol default fallback
    return f"NASDAQ:{ticker_clean}"


def get_tradingview_ticker_tape_symbols(
    indices: dict | None = None,
    stocks: list[dict] | None = None,
    limit: int = 30,
) -> list[dict[str, str]]:
    """Build a list of symbol configuration objects for the TradingView Ticker Tape widget.

    Args:
        indices: Dict of live index payloads keyed by header name (e.g. ``{"N225": {...}}``).
        stocks: List of stock payload dicts (``symbol``/``name`` keys).
        limit: Maximum number of tape symbols to return.

    Returns:
        List of dicts formatted as [{"proName": "TSE:7203", "title": "トヨタ"}, ...]
    """
    results: list[dict[str, str]] = []
    seen_pro_names = set()

    # 1. Watchlist symbols requested to appear on the tape bar
    watchlist_defaults = [
        {"proName": "INDEX:NKY", "title": "日経225", "description": "日経225"},
        {"proName": "FOREXCOM:DJI", "title": "ダウ平均", "description": "ダウ平均"},
        {"proName": "FOREXCOM:SPXUSD", "title": "S&P 500", "description": "S&P 500"},
        {"proName": "FOREXCOM:NSXUSD", "title": "ナスダック総合", "description": "ナスダック総合"},
        {"proName": "CAPITALCOM:DXY", "title": "ドルインデックス", "description": "ドルインデックス"},
        {"proName": "CAPITALCOM:VIX", "title": "VIX指数", "description": "VIX指数"},
        {"proName": "OTC:SFTBY", "title": "ソフトバンクG", "description": "ソフトバンクG"},
        {"proName": "NYSE:MUFG", "title": "三菱UFJ", "description": "三菱UFJ"},
        {"proName": "NYSE:SONY", "title": "ソニーG", "description": "ソニーG"},
        {"proName": "TVC:GOLD", "title": "金", "description": "金"},
        {"proName": "TVC:USOIL", "title": "原油", "description": "原油"},
        {"proName": "FOREXCOM:JP225", "title": "日経225先物", "description": "日経225先物"},
        {"proName": "FRED:DGS10", "title": "米10年債", "description": "米10年債"},
        {"proName": "FX:USDJPY", "title": "ドル円", "description": "ドル円"},
        {"proName": "FX:EURUSD", "title": "ユーロドル", "description": "ユーロドル"},
        {"proName": "FX:GBPJPY", "title": "ポンド円", "description": "ポンド円"},
        {"proName": "BITSTAMP:BTCUSD", "title": "BTC/USD", "description": "BTC/USD"},
        {"proName": "BITFLYER:BTCJPY", "title": "BTC/JPY", "description": "BTC/JPY"},
        {"proName": "COINBASE:BTCUSD", "title": "BTCUSD.P", "description": "BTCUSD.P"},
    ]

    for item in watchlist_defaults:
        if len(results) >= limit:
            break
        results.append(item)
        seen_pro_names.add(item["proName"])

    # 2. Add indices present in the live payload (header key -> symbol),
    #    deduplicated against the watchlist defaults.
    if indices:
        for header_key in indices:
            if len(results) >= limit:
                break
            index_symbol = _INDEX_HEADER_TO_SYMBOL.get(str(header_key))
            if not index_symbol or index_symbol not in INDEX_MAP:
                continue
            index_info = INDEX_MAP[index_symbol]
            pro_name = index_info["proName"]
            if pro_name in seen_pro_names:
                continue
            results.append({"proName": pro_name, "title": index_info["title"], "description": index_info.get("description", index_info["title"])})
            seen_pro_names.add(pro_name)

    # 3. Add custom stocks passed in (excluding Japanese TSE stock tickers as TradingView free embed widgets block TSE equity feeds)
    if stocks:
        for item in stocks:
            if len(results) >= limit:
                break
            symbol = item.get("symbol") or item.get("ticker", "")
            name = item.get("name") or symbol
            if not symbol:
                continue
            # Skip Japanese TSE equity tickers (.T / TSE:XXXX) to prevent invalid symbol errors in embed widget
            if symbol.strip().upper().endswith(".T"):
                continue
            pro_name = item.get("tv_symbol") or get_tradingview_symbol(symbol, exchange=item.get("exchange"))
            if pro_name.startswith("TSE:") and pro_name != "TSE:TOPIX":
                continue
            if pro_name and pro_name not in seen_pro_names:
                results.append({"proName": pro_name, "title": name, "description": name})
                seen_pro_names.add(pro_name)

    return results

