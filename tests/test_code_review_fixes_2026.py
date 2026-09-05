"""Regression tests for code review fixes (cache isolation, SSE announcement, extract_chat_content, UI accessibility)."""

from pathlib import Path
from unittest.mock import patch

from app_state import app_state
from route_helpers import remove_stock_from_caches
from utils.validators import extract_chat_content


class NonSerializableObj:
    """An object that raises TypeError on json.dumps."""

    def __repr__(self):
        return "<NonSerializableObj>"


def test_remove_stock_from_caches_no_collateral_damage(client):
    """Verifies that remove_stock_from_caches('A', 'us') does NOT evict 'AAPL' or 'AMZN'."""
    with client.application.app_context():
        # Populate disk caches
        app_state.stock_disk_cache.set("hist_A", {"data": "A"})
        app_state.stock_disk_cache.set("hist_A_1mo_1d", {"data": "A_1mo"})
        app_state.stock_disk_cache.set("hist_AAPL", {"data": "AAPL"})
        app_state.stock_disk_cache.set("hist_AAPL_1mo_1d", {"data": "AAPL_1mo"})
        app_state.stock_disk_cache.set("hist_AMZN", {"data": "AMZN"})

        # Populate payload disk cache
        app_state.payload_disk_cache.set("payload_A_us", {"symbol": "A"})
        app_state.payload_disk_cache.set("payload_AAPL_us", {"symbol": "AAPL"})

        # Populate SSE memory caches
        with app_state.cache.sse_data_lock:
            app_state.market.current_stocks_cache["us"] = [
                {"symbol": "A", "name": "Agilent"},
                {"symbol": "AAPL", "name": "Apple"},
            ]
            app_state.market.target_stocks_cache["us"] = [
                {"symbol": "A", "name": "Agilent"},
                {"symbol": "AAPL", "name": "Apple"},
            ]

        # Populate yfinance short cache
        with app_state.yfinance_short_cache_lock:
            app_state.yfinance_short_cache["info_short_A"] = {"name": "Agilent"}
            app_state.yfinance_short_cache["info_short_AAPL"] = {"name": "Apple"}

        # Execute removal
        remove_stock_from_caches("A", "us")

        # Assert 'A' is removed
        assert app_state.stock_disk_cache.get("hist_A") is None
        assert app_state.stock_disk_cache.get("hist_A_1mo_1d") is None
        assert app_state.payload_disk_cache.get("payload_A_us") is None
        with app_state.yfinance_short_cache_lock:
            assert "info_short_A" not in app_state.yfinance_short_cache
            assert "info_short_AAPL" in app_state.yfinance_short_cache

        with app_state.cache.sse_data_lock:
            symbols_curr = [
                s["symbol"] for s in app_state.market.current_stocks_cache.get("us", [])
            ]
            assert "A" not in symbols_curr
            assert "AAPL" in symbols_curr

        # Assert 'AAPL' and 'AMZN' are intact on disk (no collateral prefix eviction)
        assert app_state.stock_disk_cache.get("hist_AAPL") is not None
        assert app_state.stock_disk_cache.get("hist_AAPL_1mo_1d") is not None
        assert app_state.stock_disk_cache.get("hist_AMZN") is not None
        assert app_state.payload_disk_cache.get("payload_AAPL_us") is not None


def test_api_update_portfolio_announces_both_sse_modes(client):
    """Verifies that /api/stocks/portfolio triggers both Mode 1 and Mode 2 announcements."""
    with (
        patch("routes.stocks.portfolio.announce_current_market_state") as mock_mode1,
        patch("routes.stocks.portfolio.announce_real_market_state") as mock_mode2,
        patch("routes.stocks.portfolio.schedule_sync_all_stocks_now"),
    ):
        resp = client.post(
            "/api/stocks/portfolio",
            json={
                "symbol": "AAPL",
                "market": "us",
                "shares": 10.0,
                "avg_price": 150.0,
            },
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"Origin": "http://localhost:5000"},
        )
        assert resp.status_code == 200
        assert resp.json["success"] is True
        mock_mode1.assert_called_once()
        mock_mode2.assert_called_once()


def test_extract_chat_content_with_non_serializable_object():
    """Verifies extract_chat_content does not crash with TypeError on non-serializable objects."""
    # When choices is empty and response cannot be serialized to JSON
    bad_resp = NonSerializableObj()
    res = extract_chat_content(bad_resp)
    assert res == "(AIサービスから有効な応答を取得できませんでした)"

    # When content is a list of chunks without text and contains non-serializable object
    resp_with_chunk = {"choices": [{"message": {"content": [NonSerializableObj()]}}]}
    res2 = extract_chat_content(resp_with_chunk)
    assert res2 == "(テキストの抽出に失敗しました)"


def test_accessibility_ai_drawer_input_has_aria_label():
    """Verifies templates/index.html includes an aria-label on aiDrawerInput."""
    template_path = Path(__file__).resolve().parents[1] / "templates" / "index.html"
    content = template_path.read_text(encoding="utf-8")
    assert 'id="aiDrawerInput"' in content
    assert 'aria-label="AIアナリストに質問"' in content


def test_screener_template_escapes_comparison_symbols():
    """Verifies templates/screener.html escapes < and > in button texts."""
    template_path = Path(__file__).resolve().parents[1] / "templates" / "screener.html"
    content = template_path.read_text(encoding="utf-8")
    assert "(&lt;0%)" in content
    assert "(<0%)" not in content
