from utils.tradingview_mapper import (
    INDEX_MAP,
    get_tradingview_symbol,
    get_tradingview_ticker_tape_symbols,
)


def test_get_tradingview_symbol_us_stocks():
    """Test standard US tickers mapping."""
    assert get_tradingview_symbol("AAPL") == "NASDAQ:AAPL"
    assert get_tradingview_symbol("NVDA") == "NASDAQ:NVDA"
    assert get_tradingview_symbol("msft") == "NASDAQ:MSFT"
    assert get_tradingview_symbol("IONQ") == "NYSE:IONQ"


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
