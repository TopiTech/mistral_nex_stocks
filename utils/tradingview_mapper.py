"""utils/tradingview_mapper.py - TradingView symbol mapping utility.

Converts internal stock tickers (e.g., 7203.T, AAPL, ^GSPC, ^N225) into official
TradingView exchange-prefixed symbol identifiers (e.g., TSE:7203, NASDAQ:AAPL, FOREXCOM:SPXUSD).
"""

import logging
import threading

logger = logging.getLogger(__name__)

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
    # Popular/common NYSE stocks as pre-populated safety cache
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
    "NKE": "NYSE",
    "DIS": "NYSE",
    "WMT": "NYSE",
    "LLY": "NYSE",
    "ORCL": "NYSE",
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
    "GE": "NYSE",
    "T": "NYSE",
    "C": "NYSE",
    "F": "NYSE",
    "X": "NYSE",
    "CAT": "NYSE",
    "MMM": "NYSE",
    "LMT": "NYSE",
    "RTX": "NYSE",
    "BA": "NYSE",
    "GS": "NYSE",
    "MS": "NYSE",
    "WFC": "NYSE",
    "SCHW": "NYSE",
    "AMT": "NYSE",
    "SPG": "NYSE",
    "LOW": "NYSE",
    "DE": "NYSE",
    "SYK": "NYSE",
    "MDT": "NYSE",
    "EL": "NYSE",
    "CL": "NYSE",
    "KMB": "NYSE",
    "MO": "NYSE",
    "PM": "NYSE",
    "M": "NYSE",
    "L": "NYSE",
    "W": "NYSE",
    "K": "NYSE",
    "U": "NYSE",
    "AI": "NYSE",
    "UBER": "NYSE",
    "LYFT": "NYSE",
    "RBLX": "NYSE",
    "NIO": "NYSE",
    "XPEV": "NYSE",
    "TSM": "NYSE",
    "RKT": "NYSE",
    "SNOW": "NYSE",
    "NET": "NYSE",
    "PATH": "NYSE",
    "SPOT": "NYSE",
    "SQ": "NYSE",
    "SHOP": "NYSE",
    "SNAP": "NYSE",
    "TWLO": "NYSE",
    "DELL": "NYSE",
    "NOW": "NYSE",
    # Popular NASDAQ stocks
    "PLTR": "NASDAQ",
    "COIN": "NASDAQ",
    "LI": "NASDAQ",
    "ASML": "NASDAQ",
    "AAPL": "NASDAQ",
    "NVDA": "NASDAQ",
    "MSFT": "NASDAQ",
    "AMZN": "NASDAQ",
    "META": "NASDAQ",
    "GOOGL": "NASDAQ",
    "GOOG": "NASDAQ",
    "TSLA": "NASDAQ",
    "AMD": "NASDAQ",
    "INTC": "NASDAQ",
    "QCOM": "NASDAQ",
    "AVGO": "NASDAQ",
    "TXN": "NASDAQ",
    "AMAT": "NASDAQ",
    "MU": "NASDAQ",
    "CSCO": "NASDAQ",
    "ADBE": "NASDAQ",
    "NFLX": "NASDAQ",
    "PYPL": "NASDAQ",
    "COST": "NASDAQ",
    "PEP": "NASDAQ",
    "TMUS": "NASDAQ",
    "CMCSA": "NASDAQ",
    "HON": "NASDAQ",
    "AMGN": "NASDAQ",
    "SBUX": "NASDAQ",
    "MDLZ": "NASDAQ",
    "ISRG": "NASDAQ",
    "BKNG": "NASDAQ",
    "GILD": "NASDAQ",
    "ADP": "NASDAQ",
    "VRTX": "NASDAQ",
    "REGN": "NASDAQ",
    "PANW": "NASDAQ",
    "KLAC": "NASDAQ",
    "LRCX": "NASDAQ",
    "SNPS": "NASDAQ",
    "CDNS": "NASDAQ",
    "CRWD": "NASDAQ",
    "MAR": "NASDAQ",
    "ORLY": "NASDAQ",
    "ABNB": "NASDAQ",
    "MNST": "NASDAQ",
    "MELI": "NASDAQ",
    "CTAS": "NASDAQ",
    "LULU": "NASDAQ",
    "MRVL": "NASDAQ",
    "WDAY": "NASDAQ",
    "FTNT": "NASDAQ",
    "DXCM": "NASDAQ",
    "SMCI": "NASDAQ",
    "ARM": "NASDAQ",
    "HOOD": "NASDAQ",
    "AFRM": "NASDAQ",
    "DDOG": "NASDAQ",
    "MDB": "NASDAQ",
    "ZS": "NASDAQ",
    "SOFI": "NASDAQ",
    "MSTR": "NASDAQ",
    "MARA": "NASDAQ",
    "RIOT": "NASDAQ",
    "CLSK": "NASDAQ",
    "PDD": "NASDAQ",
    "JD": "NASDAQ",
    "BIDU": "NASDAQ",
    "SE": "NASDAQ",
    "GRAB": "NASDAQ",
    "CELH": "NASDAQ",
    "APP": "NASDAQ",
    "DKNG": "NASDAQ",
    "ON": "NASDAQ",
    "ROKU": "NASDAQ",
}

_CACHE_LOCK = threading.Lock()


def resolve_exchange_prefix(exchange: str | None) -> str | None:
    """Resolve Yahoo Finance or data provider exchange code/name to TradingView exchange prefix.

    Examples:
        - "NYQ" / "NYSE" / "NYS" / "PCX" / "NYSEArca" -> "NYSE"
        - "NMS" / "NGM" / "NCM" / "NASDAQ" / "NasdaqGS" -> "NASDAQ"
        - "ASE" / "AMEX" -> "AMEX"
        - "TSE" / "TYO" / "JPX" -> "TSE"
    """
    if not exchange or not isinstance(exchange, str):
        return None
    ex = exchange.strip().upper()
    clean_ex = ex.replace(" ", "").replace("-", "").replace("_", "")

    if clean_ex in (
        "NYQ",
        "NYSE",
        "NYS",
        "NYE",
        "PCX",
        "ARC",
        "ARCA",
        "NYSEARCA",
        "NYSEMKT",
        "NEWYORKSTOCKEXCHANGE",
        "NEWYORKSTOCKEXCHANGEINC",
    ) or ex in (
        "NEW YORK STOCK EXCHANGE",
        "NEW YORK STOCK EXCHANGE, INC.",
        "NYSE ARCA",
        "NYSE MKT",
    ):
        return "NYSE"

    if clean_ex in (
        "NMS",
        "NGM",
        "NCM",
        "NAS",
        "NASDAQ",
        "NASDAQGS",
        "NASDAQGM",
        "NASDAQCM",
        "NASDAQSTOCKMARKET",
    ) or ex in (
        "NASDAQ STOCK MARKET",
    ):
        return "NASDAQ"

    if clean_ex in ("ASE", "AMEX", "NYSEAMERICAN") or ex in ("NYSE AMERICAN",):
        return "AMEX"

    if clean_ex in ("TSE", "TYO", "JPX", "TOKYO"):
        return "TSE"

    # "INDEX" is the prefix this app already emits for index tickers
    # (get_tradingview_symbol_meta step 4). Accepting it here lets the
    # symbol-heuristic exchange resolver register index mappings without
    # a network lookup.
    if clean_ex in ("INDEX",):
        return "INDEX"

    if clean_ex in ("PNK", "PINK", "OTC", "OTCMKTS", "OTCBB"):
        return "OTC"

    if clean_ex in ("BAT", "BATS", "CBOE"):
        return "BATS"

    if clean_ex in ("IEX",):
        return "IEX"

    if any(k in clean_ex for k in ("NYSE", "NYQ", "ARCA", "PCX", "NYE")):
        return "NYSE"
    if any(k in clean_ex for k in ("NASDAQ", "NMS", "NGM", "NCM", "NAS")):
        return "NASDAQ"
    if any(k in clean_ex for k in ("AMEX", "AMERICAN", "ASE")):
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


def is_ticker_exchange_cached(ticker: str) -> bool:
    """Return True when a resolved exchange prefix is already cached for the ticker.

    Lets callers (e.g. stock_provider exchange extraction) skip network-backed
    lookups once the mapping is known, avoiding repeat yfinance requests.
    """
    if not ticker:
        return False
    clean_ticker = ticker.strip().upper()
    with _CACHE_LOCK:
        return clean_ticker in _TICKER_EXCHANGE_CACHE


def get_ticker_exchange(ticker: str) -> str | None:
    """Return the cached, resolved exchange prefix for a ticker, or None.

    Cache-only lookup: never performs network I/O.
    """
    if not is_ticker_exchange_cached(ticker):
        return None
    clean_ticker = ticker.strip().upper()
    with _CACHE_LOCK:
        return _TICKER_EXCHANGE_CACHE.get(clean_ticker)


def _resolve_ticker_exchange_dynamically(ticker: str) -> str | None:
    """Attempt dynamic lookup of exchange code for a given ticker via cache or symbol heuristics.

    This function is cache-only and performs NO synchronous network I/O.
    """
    clean_ticker = ticker.strip().upper()

    # 1. Check in-memory cache
    with _CACHE_LOCK:
        if clean_ticker in _TICKER_EXCHANGE_CACHE:
            return _TICKER_EXCHANGE_CACHE[clean_ticker]

    # 2. Check stock payload info cache (cache-only: never fetches from network)
    try:
        from utils.stock_payload import get_stock_info_cached

        cached_info = get_stock_info_cached(clean_ticker, cache_only=True)
        if isinstance(cached_info, dict) and cached_info.get("exchange"):
            prefix = resolve_exchange_prefix(cached_info.get("exchange"))
            if prefix:
                register_ticker_exchange(clean_ticker, prefix)
                return prefix
    except Exception as exc:
        logger.debug("Failed to resolve ticker exchange dynamically for %s: %s", clean_ticker, exc)

    # 3. Apply US Ticker Heuristics (dots/dashes share classes or 1-2 character US symbols default to NYSE)
    if not clean_ticker.startswith("^") and not clean_ticker.endswith(".T") and ":" not in clean_ticker:
        if "." in clean_ticker or "-" in clean_ticker:
            register_ticker_exchange(clean_ticker, "NYSE")
            return "NYSE"
        if len(clean_ticker) <= 2 and clean_ticker.isalpha():
            register_ticker_exchange(clean_ticker, "NYSE")
            return "NYSE"

    return None


def get_tradingview_symbol_meta(ticker: str, exchange: str | None = None) -> tuple[str, bool, str | None]:
    """Convert ticker to TradingView symbol, returning (tv_symbol, is_fallback, resolved_prefix)."""
    if not ticker:
        return ("", False, None)

    ticker_clean = ticker.strip().upper()

    # 1. Check Index Map
    if ticker_clean in INDEX_MAP:
        return (INDEX_MAP[ticker_clean]["proName"], False, "INDEX")

    # 2. Check Japanese Stock (.T suffix)
    if ticker_clean.endswith(".T"):
        code = ticker_clean[:-2]
        return (f"TSE:{code}", False, "TSE")

    # 3. Check US Exchange manual overrides
    if ticker_clean in US_STOCK_EXCHANGE_MAP:
        pref = US_STOCK_EXCHANGE_MAP[ticker_clean]
        return (f"{pref}:{ticker_clean}", False, pref)

    # 4. Known US Index symbols
    if ticker_clean.startswith("^"):
        symbol_name = ticker_clean[1:]
        return (f"INDEX:{symbol_name}", False, "INDEX")

    # 5. Explicit exchange argument provided
    prefix = resolve_exchange_prefix(exchange)
    if prefix:
        register_ticker_exchange(ticker_clean, prefix)
        return (f"{prefix}:{ticker_clean}", False, prefix)

    # 6. Resolve exchange dynamically via cache / stock info / heuristics
    dynamic_prefix = _resolve_ticker_exchange_dynamically(ticker_clean)
    if dynamic_prefix:
        return (f"{dynamic_prefix}:{ticker_clean}", False, dynamic_prefix)

    # Standard US stock symbol default fallback (NASDAQ fallback)
    return (f"NASDAQ:{ticker_clean}", True, "NASDAQ")


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
    tv_symbol, _, _ = get_tradingview_symbol_meta(ticker, exchange)
    return tv_symbol



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
