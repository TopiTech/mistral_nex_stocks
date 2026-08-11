# tests/test_nikkei225jp_scraper.py
"""Unit tests for Nikkei225JPScraper, Nikkei225JPProvider, and prioritized fallback integration."""

import time
from unittest.mock import MagicMock, patch

from services.fallback_provider import (
    CompositeFallbackProvider,
    Nikkei225JPProvider,
)
from services.realtime_engine import (
    Nikkei225JPScraper,
    RealtimeMarketEngine,
)

SAMPLE_ADR_ALL_JS = """
var Shu="6758,7203,9984";
var A0=new Array();q=0;
A0[q]="7203_TM_トヨタ自動車_17_Toyota Motor_NYSE_0.1_08/10_2981_+1_+0.03_0_04:55_188.54_-1.55_-0.82_272356_3003_159.278_23:56_2993_36,200_S_1";q++;
A0[q]="6758_SONY_ソニーグループ_16_Sony Group_NYSE_1_08/10_3764_+43_+1.16_0_04:56_23.80_0.34_+1.43_3243140_3791_159.284_03:07_3800_15,400_S_1";q++;
A0[q]="9984_SFTBY_ソフトバンクグループ_25_Softbank Group_OTC_2_08/10_5484_-68_-1.22_0_04:59_16.80_-0.80_-4.55_1663115_5352_159.292_23:59_5401_64,100_U_1";q++;
"""

SAMPLE_INDEX_MID_JS = """
A[111]="38000.50_+250.00_+0.66_08/10_0";
A[211]="40000.00_-100.00_-0.25_08/10_0";
A[212]="17500.00_+50.00_+0.29_08/10_0";
A[213]="5500.00_+15.00_+0.27_08/10_0";
A[511]="155.50_+0.20_+0.13_17:33_1";
A[514]="170.00_-0.10_-0.06_17:33_1";
"""

SAMPLE_INDEX_BTM_JS = """
A[1001]="9500000_+100000_+1.06_17:33_1";
A[511]="155.50_+0.20_+0.13_17:33_1";
"""


def test_nikkei225jp_scraper_fetch_quote_regular():
    """Nikkei225JPScraper must parse regular JP stock quotes from _adr_all.js."""
    scraper = Nikkei225JPScraper()
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = SAMPLE_ADR_ALL_JS.encode("utf-8")
    mock_session.get.return_value = mock_resp

    with patch.object(scraper, "_get_session", return_value=mock_session):
        payload = scraper.fetch_quote("7203.T")
        assert payload is not None
        assert payload["symbol"] == "7203.T"
        assert payload["price"] == 2981.0
        assert payload["change"] == 1.0
        assert payload["change_percent"] == 0.03
        assert payload["source"] == "nikkei225jp_adr"


def test_nikkei225jp_scraper_fetch_pts_quote():
    """Nikkei225JPScraper must parse PTS quotes from _adr_all.js."""
    scraper = Nikkei225JPScraper()
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = SAMPLE_ADR_ALL_JS.encode("utf-8")
    mock_session.get.return_value = mock_resp

    with patch.object(scraper, "_get_session", return_value=mock_session):
        payload = scraper.fetch_pts_quote("7203.T")
        assert payload is not None
        assert payload["symbol"] == "7203.T"
        assert payload["price"] == 2993.0
        assert payload["volume"] == 36200
        assert payload["pts_time"] == "23:56"
        assert payload["pts"] is True
        assert payload["source"] == "nikkei225jp_pts"
        # PTS change relative to Tokyo price (2993 - 2981 = 12)
        assert payload["change"] == 12.0


def test_nikkei225jp_scraper_fetch_index_quote():
    """Nikkei225JPScraper must parse index quotes from ajax_TOP_mid.js."""
    scraper = Nikkei225JPScraper()
    mock_session = MagicMock()

    def _mock_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "ajax_TOP_mid" in url:
            resp.content = SAMPLE_INDEX_MID_JS.encode("utf-8")
        elif "ajax_TOP_btm" in url:
            resp.content = SAMPLE_INDEX_BTM_JS.encode("utf-8")
        else:
            resp.content = b""
        return resp

    mock_session.get.side_effect = _mock_get

    with patch.object(scraper, "_get_session", return_value=mock_session):
        # Nikkei 225
        n225 = scraper.fetch_quote("^N225")
        assert n225 is not None
        assert n225["symbol"] == "^N225"
        assert n225["price"] == 38000.50
        assert n225["change"] == 250.00
        assert n225["source"] == "nikkei225jp"

        # USDJPY
        usdjpy = scraper.fetch_quote("USDJPY=X")
        assert usdjpy is not None
        assert usdjpy["price"] == 155.50
        assert usdjpy["change"] == 0.20

        # NY Dow
        dji = scraper.fetch_quote("^DJI")
        assert dji is not None
        assert dji["price"] == 40000.00
        assert dji["change"] == -100.00


def test_nikkei225jp_scraper_failure_and_cooldown():
    """Nikkei225JPScraper must track failures and enter cooldown."""
    scraper = Nikkei225JPScraper()
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_session.get.return_value = mock_resp

    with patch.object(scraper, "_get_session", return_value=mock_session):
        for _ in range(3):
            res = scraper.fetch_quote("9999.T")
            assert res is None
        assert scraper._is_in_cooldown("9999.T")


def test_nikkei225jp_scraper_remove_symbol():
    """Nikkei225JPScraper remove_symbol purges tracking state."""
    scraper = Nikkei225JPScraper()
    scraper._consecutive_failures["7203.T:regular"] = 3
    scraper._last_failure_time["7203.T:regular"] = time.time()
    scraper._structure_change_reported.add("7203.T:regular")

    scraper.remove_symbol("7203.T")
    assert "7203.T:regular" not in scraper._consecutive_failures
    assert "7203.T:regular" not in scraper._structure_change_reported


def test_nikkei225jp_provider_fallback():
    """Nikkei225JPProvider in fallback_provider.py returns quote dict."""
    provider = Nikkei225JPProvider()
    mock_session = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = SAMPLE_ADR_ALL_JS
    mock_resp.content = SAMPLE_ADR_ALL_JS.encode("utf-8")
    mock_session.get.return_value = mock_resp

    with patch.object(provider, "_get_client", return_value=(mock_session, True)):
        quote = provider.get_latest_quote("7203.T")
        assert quote is not None
        assert quote["symbol"] == "7203.T"
        assert quote["regularMarketPrice"] == 2981.0
        assert quote["source"] == "nikkei225jp_adr"


def test_composite_fallback_priority_chain():
    """CompositeFallbackProvider must try Yahoo JP -> Nikkei225JP -> Minkabu (lowest tier)."""
    comp = CompositeFallbackProvider()

    # 1. AlphaVantage disabled/no key
    comp.alpha_vantage.get_latest_quote = MagicMock(return_value=None)
    comp.yahoo_jp.get_latest_quote = MagicMock(return_value=None)
    comp.nikkei225jp.get_latest_quote = MagicMock(return_value={"symbol": "7203.T", "regularMarketPrice": 2981.0, "source": "nikkei225jp_adr"})
    comp.minkabu.get_latest_quote = MagicMock(return_value={"symbol": "7203.T", "regularMarketPrice": 2950.0, "source": "minkabu"})

    quote = comp.get_latest_quote("7203.T")
    assert quote is not None
    assert quote["source"] == "nikkei225jp_adr"
    comp.yahoo_jp.get_latest_quote.assert_called_once_with("7203.T")
    comp.nikkei225jp.get_latest_quote.assert_called_once_with("7203.T")
    # Minkabu must NOT have been called because Nikkei225JP succeeded
    comp.minkabu.get_latest_quote.assert_not_called()

    # 2. When Nikkei225JP also fails, Minkabu is called as last resort
    comp.nikkei225jp.get_latest_quote.return_value = None
    quote_minkabu = comp.get_latest_quote("7203.T")
    assert quote_minkabu is not None
    assert quote_minkabu["source"] == "minkabu"
    comp.minkabu.get_latest_quote.assert_called_once_with("7203.T")


def test_realtime_engine_prioritized_fallback_order():
    """RealtimeMarketEngine must try Yahoo JP -> SBI -> Nikkei225JP -> Minkabu as lowest fallback."""
    engine = RealtimeMarketEngine()

    # Test regular quote fallback chain
    engine.yahoojp_scraper.fetch_jp_symbol = MagicMock(return_value=None)
    engine.sbi_scraper.fetch_quote = MagicMock(return_value=None)
    engine.nikkei225jp_scraper.fetch_quote = MagicMock(return_value={"symbol": "7203.T", "price": 2981.0, "source": "nikkei225jp_adr"})
    engine.minkabu_scraper.fetch_quote = MagicMock(return_value={"symbol": "7203.T", "price": 2950.0, "source": "minkabu"})

    payload = engine.yahoojp_scraper._fetch_regular_with_fallback("7203.T")
    assert payload is not None
    assert payload["source"] == "nikkei225jp_adr"
    engine.sbi_scraper.fetch_quote.assert_called_once_with("7203.T")
    engine.nikkei225jp_scraper.fetch_quote.assert_called_once_with("7203.T")
    engine.minkabu_scraper.fetch_quote.assert_not_called()

    # When Nikkei225JP fails, Minkabu is called
    engine.nikkei225jp_scraper.fetch_quote.return_value = None
    payload_minkabu = engine.yahoojp_scraper._fetch_regular_with_fallback("7203.T")
    assert payload_minkabu is not None
    assert payload_minkabu["source"] == "minkabu"
    engine.minkabu_scraper.fetch_quote.assert_called_once_with("7203.T")


def test_realtime_engine_pts_prioritized_fallback_order():
    """RealtimeMarketEngine _fetch_pts_with_fallback must try Yahoo JP PTS -> SBI PTS -> Nikkei225JP PTS -> Minkabu PTS."""
    engine = RealtimeMarketEngine()

    engine.yahoojp_scraper.fetch_pts_symbol = MagicMock(return_value=None)
    engine.sbi_scraper.fetch_pts_quote = MagicMock(return_value=None)
    engine.nikkei225jp_scraper.fetch_pts_quote = MagicMock(return_value={"symbol": "7203.T", "price": 2993.0, "pts": True, "source": "nikkei225jp_pts"})
    engine.minkabu_scraper.fetch_pts_quote = MagicMock(return_value={"symbol": "7203.T", "price": 2950.0, "pts": True, "source": "minkabu_pts"})

    pts_payload = engine._fetch_pts_with_fallback("7203.T")
    assert pts_payload is not None
    assert pts_payload["source"] == "nikkei225jp_pts"
    engine.sbi_scraper.fetch_pts_quote.assert_called_once_with("7203.T")
    engine.nikkei225jp_scraper.fetch_pts_quote.assert_called_once_with("7203.T")
    engine.minkabu_scraper.fetch_pts_quote.assert_not_called()

    # When Nikkei225JP PTS fails, Minkabu PTS is called as last resort
    engine.nikkei225jp_scraper.fetch_pts_quote.return_value = None
    pts_minkabu = engine._fetch_pts_with_fallback("7203.T")
    assert pts_minkabu is not None
    assert pts_minkabu["source"] == "minkabu_pts"
    engine.minkabu_scraper.fetch_pts_quote.assert_called_once_with("7203.T")
