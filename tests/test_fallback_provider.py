from unittest.mock import MagicMock, patch

from services.fallback_provider import (
    AlphaVantageProvider,
    CompositeFallbackProvider,
    MinkabuProvider,
    Nikkei225JPProvider,
    YahooJPScraperProvider,
    YahooWebScraperProvider,
)


@patch("services.fallback_provider.get_alphavantage_api_key")
@patch("requests.get")
def test_alphavantage_provider_success(mock_get, mock_get_key):
    mock_get_key.return_value = "TESTKEY"
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "Global Quote": {
            "01. symbol": "AAPL",
            "02. open": "150.00",
            "03. high": "155.00",
            "04. low": "149.00",
            "05. price": "154.00",
            "06. volume": "100000",
            "07. latest trading day": "2023-10-01",
            "08. previous close": "148.00",
        }
    }
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    provider = AlphaVantageProvider()
    quote = provider.get_latest_quote("AAPL")

    assert quote is not None
    assert quote["symbol"] == "AAPL"
    assert quote["regularMarketPrice"] == 154.0
    assert quote["regularMarketVolume"] == 100000
    assert quote["regularMarketOpen"] == 150.0


@patch("services.fallback_provider.get_alphavantage_api_key")
def test_alphavantage_provider_no_key(mock_get_key):
    mock_get_key.return_value = None
    provider = AlphaVantageProvider()
    quote = provider.get_latest_quote("AAPL")
    assert quote is None


@patch("services.fallback_provider.get_alphavantage_api_key")
@patch("requests.get")
def test_alphavantage_provider_japanese_symbol(mock_get, mock_get_key):
    mock_get_key.return_value = "TESTKEY"
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "Global Quote": {"01. symbol": "7203.T", "05. price": "2500.0", "06. volume": "50000"}
    }
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    provider = AlphaVantageProvider()
    quote = provider.get_latest_quote("7203.T")

    assert quote is not None
    assert quote["symbol"] == "7203.T"
    assert quote["regularMarketPrice"] == 2500.0
    # Ensure requests.get was called with 7203.T (not converted to .TRK)
    mock_get.assert_called_once()
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["params"]["symbol"] == "7203.T"


@patch("services.fallback_provider.get_alphavantage_api_key")
@patch("requests.get")
def test_alphavantage_provider_rate_limit_note(mock_get, mock_get_key):
    mock_get_key.return_value = "TESTKEY"
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "Note": "Thank you for using Alpha Vantage! Our standard API call frequency is 5 calls per minute..."
    }
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    provider = AlphaVantageProvider()
    quote = provider.get_latest_quote("AAPL")
    assert quote is None


@patch("services.fallback_provider.get_alphavantage_api_key")
@patch("requests.get")
def test_alphavantage_provider_error_message(mock_get, mock_get_key):
    mock_get_key.return_value = "TESTKEY"
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "Error Message": "Invalid API call. Please check your parameters..."
    }
    mock_resp.raise_for_status.return_value = None
    mock_get.return_value = mock_resp

    provider = AlphaVantageProvider()
    quote = provider.get_latest_quote("INVALID_TICKER")
    assert quote is None


def test_yahoo_web_scraper_init_no_curl_cffi():
    with patch.dict("sys.modules", {"curl_cffi": None}):
        provider = YahooWebScraperProvider()
        assert provider.requests is None


def test_yahoo_web_scraper_success():
    mock_requests = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = 'root.App.main = {"context": {"dispatcher": {"stores": {"QuoteSummaryStore": {"price": {"regularMarketPrice": {"raw": 150.5}, "regularMarketPreviousClose": {"raw": 149.5}, "regularMarketVolume": {"raw": 200000}, "regularMarketOpen": {"raw": 149.8}, "regularMarketDayHigh": {"raw": 151.0}, "regularMarketDayLow": {"raw": 149.0}}}}}}}; (function'
    mock_requests.get.return_value = mock_resp

    provider = YahooWebScraperProvider()
    provider.session = mock_requests
    quote = provider.get_latest_quote("AAPL")

    assert quote is not None
    assert quote["symbol"] == "AAPL"
    assert quote["regularMarketPrice"] == 150.5
    assert quote["regularMarketPreviousClose"] == 149.5
    assert quote["regularMarketVolume"] == 200000


def test_yahoo_web_scraper_html_marker_does_not_fake_prev_close():
    """R4: the HTML marker fallback must NOT set previous close == current price.

    Faking prev_close=price forces change=0 and corrupts realtime change
    calculations; the previous close must be None when unknown.
    """
    mock_requests = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<html><body><span data-testid="qsp-price">42.50</span></body></html>'
    mock_requests.get.return_value = mock_resp

    provider = YahooWebScraperProvider()
    provider.session = mock_requests
    quote = provider.get_latest_quote("XYZ")

    assert quote is not None
    assert quote["regularMarketPrice"] == 42.5
    assert quote["regularMarketPreviousClose"] is None


def test_yahoo_jp_scraper_data_testid_fallback():
    """L-1: data-testid attribute selector works when the hashed class is renamed."""
    mock_requests = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<html><body><span data-testid="stock-price">3,200.75</span></body></html>'
    mock_requests.get.return_value = mock_resp

    provider = YahooJPScraperProvider()
    provider.session = mock_requests
    quote = provider.get_latest_quote("7203.T")

    assert quote is not None
    assert quote["regularMarketPrice"] == 3200.75


def test_yahoo_jp_scraper_unknown_markup_returns_none():
    """L-1: unrecognized markup with no price label safely returns None."""
    mock_requests = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<html><body><span class="SomeRenamedClass">3,200.75</span></body></html>'
    mock_requests.get.return_value = mock_resp

    provider = YahooJPScraperProvider()
    provider.session = mock_requests
    quote = provider.get_latest_quote("7203.T")

    assert quote is None


def test_yahoo_jp_scraper_current_value_label_fallback():
    """L-1: regex fallback anchored on the 現在値 label when no selector matches."""
    mock_requests = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = (
        '<html><body><div>現在値</div><span class="TotallyNewClass">4,100.25</span></body></html>'
    )
    mock_requests.get.return_value = mock_resp

    provider = YahooJPScraperProvider()
    provider.session = mock_requests
    quote = provider.get_latest_quote("9984.T")

    assert quote is not None
    assert quote["regularMarketPrice"] == 4100.25


def test_yahoo_jp_scraper_yen_prefix_fallback():
    """L-1: regex fallback anchored on the yen sign as a last resort."""
    mock_requests = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "<html><body>¥1,950.50</body></html>"
    mock_requests.get.return_value = mock_resp

    provider = YahooJPScraperProvider()
    provider.session = mock_requests
    quote = provider.get_latest_quote("6758.T")

    assert quote is not None
    assert quote["regularMarketPrice"] == 1950.5


def test_yahoo_jp_scraper_success():
    mock_requests = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '<html><body><span class="_3rXWJKZF">2,500.5</span></body></html>'
    mock_requests.get.return_value = mock_resp

    provider = YahooJPScraperProvider()
    provider.session = mock_requests
    quote = provider.get_latest_quote("7203.T")

    assert quote is not None
    assert quote["symbol"] == "7203.T"
    assert quote["regularMarketPrice"] == 2500.5


@patch.object(AlphaVantageProvider, "get_latest_quote")
@patch.object(YahooWebScraperProvider, "get_latest_quote")
def test_composite_provider_alpha_first(mock_yahoo, mock_alpha):
    mock_alpha.return_value = {"symbol": "AAPL", "regularMarketPrice": 150.0}

    provider = CompositeFallbackProvider()
    quote = provider.get_latest_quote("AAPL")

    assert quote is not None
    assert quote["regularMarketPrice"] == 150.0
    mock_yahoo.assert_not_called()


@patch.object(AlphaVantageProvider, "get_latest_quote")
@patch.object(YahooWebScraperProvider, "get_latest_quote")
def test_composite_provider_fallback_to_yahoo(mock_yahoo, mock_alpha):
    mock_alpha.return_value = None
    mock_yahoo.return_value = {"symbol": "AAPL", "regularMarketPrice": 160.0}

    provider = CompositeFallbackProvider()
    quote = provider.get_latest_quote("AAPL")

    assert quote is not None
    assert quote["regularMarketPrice"] == 160.0
    mock_alpha.assert_called_once_with("AAPL")
    mock_yahoo.assert_called_once_with("AAPL")


@patch.object(AlphaVantageProvider, "get_latest_quote")
@patch.object(YahooJPScraperProvider, "get_latest_quote")
@patch.object(Nikkei225JPProvider, "get_latest_quote")
@patch.object(MinkabuProvider, "get_latest_quote")
def test_composite_provider_jp_stock_priority(mock_minkabu, mock_nikkei, mock_yahoo_jp, mock_alpha):
    mock_alpha.return_value = None
    mock_yahoo_jp.return_value = None
    mock_nikkei.return_value = {
        "symbol": "7203.T",
        "regularMarketPrice": 2981.0,
        "source": "nikkei225jp_adr",
    }
    mock_minkabu.return_value = {
        "symbol": "7203.T",
        "regularMarketPrice": 2900.0,
        "source": "minkabu",
    }

    provider = CompositeFallbackProvider()
    quote = provider.get_latest_quote("7203.T")

    assert quote is not None
    assert quote["regularMarketPrice"] == 2981.0
    assert quote["source"] == "nikkei225jp_adr"
    mock_yahoo_jp.assert_called_once_with("7203.T")
    mock_nikkei.assert_called_once_with("7203.T")
    mock_minkabu.assert_not_called()


@patch.object(AlphaVantageProvider, "get_latest_quote")
@patch.object(Nikkei225JPProvider, "get_latest_quote")
@patch.object(YahooWebScraperProvider, "get_latest_quote")
def test_composite_provider_index_nikkei225jp_priority(mock_yahoo_web, mock_nikkei, mock_alpha):
    mock_alpha.return_value = None
    mock_nikkei.return_value = {
        "symbol": "^N225",
        "regularMarketPrice": 38000.0,
        "source": "nikkei225jp",
    }
    mock_yahoo_web.return_value = {
        "symbol": "^N225",
        "regularMarketPrice": 37900.0,
        "source": "yahoous",
    }

    provider = CompositeFallbackProvider()
    quote = provider.get_latest_quote("^N225")

    assert quote is not None
    assert quote["regularMarketPrice"] == 38000.0
    assert quote["source"] == "nikkei225jp"
    mock_nikkei.assert_called_once_with("^N225")
    mock_yahoo_web.assert_not_called()
