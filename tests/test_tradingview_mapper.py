from unittest.mock import patch

from utils.tradingview_mapper import (
    _CACHE_LOCK,
    _TICKER_EXCHANGE_CACHE,
    INDEX_MAP,
    get_ticker_exchange,
    get_tradingview_symbol,
    get_tradingview_ticker_tape_symbols,
    is_ticker_exchange_cached,
    register_ticker_exchange,
    resolve_exchange_prefix,
)


def test_resolve_exchange_prefix():
    """Test mapping of Yahoo Finance exchange strings to TradingView prefixes."""
    assert resolve_exchange_prefix("NYQ") == "NYSE"
    assert resolve_exchange_prefix("NYSE") == "NYSE"
    assert resolve_exchange_prefix("NYSEArca") == "NYSE"
    assert resolve_exchange_prefix("PCX") == "NYSE"
    assert resolve_exchange_prefix("NMS") == "NASDAQ"
    assert resolve_exchange_prefix("NGM") == "NASDAQ"
    assert resolve_exchange_prefix("NCM") == "NASDAQ"
    assert resolve_exchange_prefix("NasdaqGS") == "NASDAQ"
    assert resolve_exchange_prefix("NasdaqGM") == "NASDAQ"
    assert resolve_exchange_prefix("NasdaqCM") == "NASDAQ"
    assert resolve_exchange_prefix("NASDAQ") == "NASDAQ"
    assert resolve_exchange_prefix("ASE") == "AMEX"
    assert resolve_exchange_prefix("AMEX") == "AMEX"
    assert resolve_exchange_prefix("TSE") == "TSE"
    assert resolve_exchange_prefix("BATS") == "BATS"
    assert resolve_exchange_prefix("IEX") == "IEX"
    assert resolve_exchange_prefix("PNK") == "OTC"
    assert resolve_exchange_prefix("UNKNOWN") is None
    assert resolve_exchange_prefix(None) is None


def test_get_tradingview_symbol_us_stocks():
    """Test US tickers mapping with dynamic exchange resolution."""
    assert get_tradingview_symbol("AAPL", exchange="NMS") == "NASDAQ:AAPL"
    assert get_tradingview_symbol("NVDA", exchange="NASDAQ") == "NASDAQ:NVDA"
    assert get_tradingview_symbol("IONQ", exchange="NYQ") == "NYSE:IONQ"
    assert get_tradingview_symbol("IONQ", exchange="NYSE") == "NYSE:IONQ"
    assert get_tradingview_symbol("IBM", exchange="NYSE") == "NYSE:IBM"
    assert get_tradingview_symbol("PLTR", exchange="NasdaqGS") == "NASDAQ:PLTR"
    # Dynamic resolution without explicit exchange specified
    assert get_tradingview_symbol("IONQ") == "NYSE:IONQ"
    assert get_tradingview_symbol("IBM") == "NYSE:IBM"
    assert get_tradingview_symbol("AAPL") == "NASDAQ:AAPL"
    assert get_tradingview_symbol("PLTR") == "NASDAQ:PLTR"
    assert get_tradingview_symbol("SMCI") == "NASDAQ:SMCI"
    assert get_tradingview_symbol("PATH") == "NYSE:PATH"
    assert get_tradingview_symbol("NET") == "NYSE:NET"


def test_get_tradingview_symbol_jp_stocks():
    """Test Japanese stocks (.T suffix) mapping."""
    assert get_tradingview_symbol("7203.T") == "TSE:7203"
    assert get_tradingview_symbol("9984.T") == "TSE:9984"
    assert get_tradingview_symbol("6758.t") == "TSE:6758"


def test_get_tradingview_symbol_indices():
    """Test major market indices mapping."""
    assert get_tradingview_symbol("^GSPC") == "FOREXCOM:SPXUSD"
    assert get_tradingview_symbol("^IXIC") == "FOREXCOM:NSXUSD"
    assert get_tradingview_symbol("^N225") == "INDEX:NKY"
    assert get_tradingview_symbol("^CUSTOM") == "INDEX:CUSTOM"


def test_ixic_title_reflects_nasdaq_composite():
    """^IXIC is the Nasdaq Composite; the tape title must not say Nasdaq 100."""
    assert INDEX_MAP["^IXIC"]["proName"] == "FOREXCOM:NSXUSD"
    assert INDEX_MAP["^IXIC"]["title"] == "ナスダック総合"


def test_get_tradingview_symbol_empty():
    """Test empty symbol handling."""
    assert get_tradingview_symbol("") == ""
    assert get_tradingview_symbol(None) == ""


def test_get_tradingview_ticker_tape_symbols():
    """Test ticker tape symbol list generator excludes TSE equities."""
    stocks = [
        {"symbol": "7203.T", "name": "トヨタ自動車"},
        {"symbol": "AAPL", "name": "Apple"},
    ]
    tape = get_tradingview_ticker_tape_symbols(stocks=stocks)
    assert isinstance(tape, list)
    assert len(tape) >= 3

    pro_names = [item["proName"] for item in tape]
    assert "INDEX:NKY" in pro_names
    assert "FOREXCOM:SPXUSD" in pro_names
    assert "TSE:7203" not in pro_names  # TSE equities excluded to prevent invalid symbol errors on embed widgets
    assert "NASDAQ:AAPL" in pro_names


def test_get_tradingview_ticker_tape_symbols_uses_indices_payload():
    """Indices present in the live payload are added to the tape, deduplicated
    against the hardcoded defaults."""
    indices = {
        "N225": {"price": 38000.0},
        "SP500": {"price": 5400.0},
        "VIX": {"price": 15.0},  # ^VIX is not in INDEX_MAP -> must be skipped
        "US10Y": {"price": 4.2},
    }
    stocks = [{"symbol": "AAPL", "name": "Apple"}]
    tape = get_tradingview_ticker_tape_symbols(indices=indices, stocks=stocks)

    pro_names = [item["proName"] for item in tape]
    assert "INDEX:NKY" in pro_names  # default + payload, deduplicated
    assert "FOREXCOM:SPXUSD" in pro_names
    assert "FRED:DGS10" in pro_names  # included via watchlist / payload
    assert "NASDAQ:AAPL" in pro_names
    assert "FOREXCOM:VIX" not in pro_names

    # Defaults that also exist in the payload must appear exactly once.
    assert pro_names.count("INDEX:NKY") == 1
    assert pro_names.count("FOREXCOM:SPXUSD") == 1


def test_get_tradingview_ticker_tape_symbols_respects_limit():
    """The limit caps the tape length even when indices + stocks exceed it."""
    indices = {"N225": {"price": 1}, "US10Y": {"price": 1}}
    stocks = [{"symbol": f"S{i:03d}", "name": ""} for i in range(20)]
    tape = get_tradingview_ticker_tape_symbols(indices=indices, stocks=stocks, limit=5)
    assert len(tape) <= 5


def test_is_ticker_exchange_cached():
    """is_ticker_exchange_cached reflects the in-memory exchange cache state."""
    with _CACHE_LOCK:
        _TICKER_EXCHANGE_CACHE.pop("CACHEPROBE", None)
    assert is_ticker_exchange_cached("CACHEPROBE") is False
    assert is_ticker_exchange_cached("cacheprobe") is False
    assert is_ticker_exchange_cached("") is False
    assert is_ticker_exchange_cached(None) is False

    register_ticker_exchange("CACHEPROBE", "NMS")
    assert is_ticker_exchange_cached("CACHEPROBE") is True
    # Case-insensitive lookup.
    assert is_ticker_exchange_cached("cacheprobe") is True
    # Registering with an unresolvable exchange does not overwrite the
    # existing entry (resolve_exchange_prefix returns None -> no write).
    register_ticker_exchange("CACHEPROBE", "NOT_A_REAL_EXCHANGE")
    assert is_ticker_exchange_cached("CACHEPROBE") is True  # still holds the earlier NMS entry

    with _CACHE_LOCK:
        _TICKER_EXCHANGE_CACHE.pop("CACHEPROBE", None)


def test_get_ticker_exchange():
    """get_ticker_exchange returns the cached resolved prefix or None."""
    with _CACHE_LOCK:
        _TICKER_EXCHANGE_CACHE.pop("CACHEPROBE2", None)
    assert get_ticker_exchange("CACHEPROBE2") is None
    assert get_ticker_exchange("") is None
    assert get_ticker_exchange(None) is None

    register_ticker_exchange("CACHEPROBE2", "NYSEArca")
    assert get_ticker_exchange("CACHEPROBE2") == "NYSE"
    assert get_ticker_exchange("cacheprobe2") == "NYSE"

    with _CACHE_LOCK:
        _TICKER_EXCHANGE_CACHE.pop("CACHEPROBE2", None)


def test_dynamic_exchange_resolution_is_cache_only_no_network():
    """P1 regression: resolving an unknown ticker's exchange must NEVER call
    yfinance synchronously (it runs inside the SSE handshake and the
    background sync loop; a network call there stalls connections and amplifies
    Yahoo rate-limit pressure). Unresolved tickers fall back to NASDAQ:."""
    # Ensure the ticker is not pre-populated in the exchange cache.
    with _CACHE_LOCK:
        _TICKER_EXCHANGE_CACHE.pop("ZZZZZ", None)

    with patch("utils.stock_payload.get_stock_info_cached", return_value={}) as mock_info, patch(
        "yfinance.Ticker"
    ) as mock_ticker:
        result = get_tradingview_symbol("ZZZZZ")
        # yfinance must never be instantiated for exchange resolution.
        mock_ticker.assert_not_called()
        mock_info.assert_called_once_with("ZZZZZ", cache_only=True)
        assert result == "NASDAQ:ZZZZZ"

    with _CACHE_LOCK:
        _TICKER_EXCHANGE_CACHE.pop("ZZZZZ", None)


def test_dynamic_exchange_resolution_uses_cached_info():
    """A cached stock-info exchange entry resolves without network calls."""
    with _CACHE_LOCK:
        _TICKER_EXCHANGE_CACHE.pop("ZZZZY", None)

    with patch(
        "utils.stock_payload.get_stock_info_cached",
        return_value={"exchange": "NYQ"},
    ) as mock_info, patch("yfinance.Ticker") as mock_ticker:
        result = get_tradingview_symbol("ZZZZY")
        mock_ticker.assert_not_called()
        mock_info.assert_called_once_with("ZZZZY", cache_only=True)
        assert result == "NYSE:ZZZZY"

    with _CACHE_LOCK:
        _TICKER_EXCHANGE_CACHE.pop("ZZZZY", None)


def test_get_internal_symbol_from_tv_symbol():
    """Verify conversion from TradingView symbols back to internal stock/index symbols."""
    from utils.normalization import normalize_symbol
    from utils.tradingview_mapper import get_internal_symbol_from_tv_symbol

    assert get_internal_symbol_from_tv_symbol("NASDAQ:NVDA") == "NVDA"
    assert get_internal_symbol_from_tv_symbol("TSE:7203") == "7203"
    assert get_internal_symbol_from_tv_symbol("FOREXCOM:SPXUSD") == "^GSPC"
    assert get_internal_symbol_from_tv_symbol("INDEX:NKY") == "^N225"
    assert get_internal_symbol_from_tv_symbol("AAPL") == "AAPL"

    # Test normalize_symbol integration
    assert normalize_symbol("NASDAQ:NVDA") == "NVDA"
    assert normalize_symbol("TSE:7203") == "7203"
    assert normalize_symbol("FOREXCOM:SPXUSD") == "^GSPC"

