"""U5 regression: D-02/D-04/D-05 + U12 (2026-10-23).

- D-02: numeric ``or`` chains must not swallow valid 0.0 (first_not_none).
- D-04: screener enrichment reports per-symbol failures via ``missing[]``.
- D-05: ``market_is_open_cached`` dead code removed.
- U12: chart OHLC slices rows before record conversion (identical output).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pandas as pd

from services import market_data_service
from services.market_data_service import _build_market_row
from utils.stock_payload import _build_chart_ohlc_data

REPO_ROOT = Path(__file__).resolve().parent.parent


def _row(**overrides):
    base = {
        "symbol": "T0",
        "name": "T Zero",
        "price": 100.0,
        "market_cap": 10_000,
        "sector": "Tech",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# D-02: valid 0.0 must survive the fallback chains
# ---------------------------------------------------------------------------


def test_d02_change_percent_zero_beats_stale_alternate_key():
    row = _build_market_row(
        "T0",
        "us",
        {"change_percent": 0.0, "regularMarketChangePercent": 5.0},
        "T Zero",
    )
    assert row["change_percent"] == 0.0


def test_d02_change_value_zero_beats_stale_alternate_key():
    row = _build_market_row(
        "T0", "us", {"change": 0.0, "regularMarketChange": 3.0}, "T Zero"
    )
    assert row["change_value"] == 0.0


def test_d02_pe_ratio_zero_beats_stale_alternate_key():
    row = _build_market_row(
        "T0", "us", {"trailingPE": 0.0, "forwardPE": 12.5}, "T Zero"
    )
    assert row["pe_ratio"] == 0.0


def test_d02_volume_zero_is_preserved_not_fallback():
    row = _build_market_row("T0", "us", {"volume": 0}, "T Zero")
    assert row["volume"] == 0


def test_d02_missing_values_still_fall_back():
    row = _build_market_row("T0", "us", {}, "T Zero")
    assert row["change_percent"] == 0.0
    assert row["change_value"] == 0.0
    assert row["pe_ratio"] is None
    assert row["volume"] == 0.0


def test_d02_no_numeric_or_fallback_in_interpolator():
    text = (REPO_ROOT / "bg" / "sse_interpolator.py").read_text(encoding="utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if "normalize_optional_number" in code and " or " in code:
            raise AssertionError(f"numeric or-chain remains at line {lineno}: {line.strip()}")


# ---------------------------------------------------------------------------
# D-04: screener reports enrichment failures via missing[]
# ---------------------------------------------------------------------------


def test_d04_screener_reports_missing_symbols():
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    aaa_row = _row(symbol="AAA", name="Alpha", price=10.0, market_cap=5000)
    with (
        app.test_client() as client,
        patch(
            "routes.stocks.views.resolve_stocks_for_response",
            return_value={"us": [], "jp": []},
        ),
        patch(
            "routes.stocks.views.build_screener_base_rows_dispatch",
            return_value=[],
        ),
        patch(
            "routes.stocks.views.build_popular_symbol_items_dispatch",
            return_value=[("AAA", "Alpha", "us"), ("BBB", "Beta", "us")],
        ),
        patch(
            "routes.stocks.views.build_screener_enrichment_dispatch",
            return_value={"AAA": aaa_row},
        ),
        patch(
            "routes.stocks.views.get_cached_dispatch",
            side_effect=lambda _key, fn, **_kw: fn(),
        ),
    ):
        resp = client.get("/api/screener")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert [s["symbol"] for s in body["stocks"]] == ["AAA"]
        assert body["missing"] == ["BBB"]


def test_d04_screener_missing_empty_when_all_enriched():
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    aaa_row = _row(symbol="AAA", name="Alpha", price=10.0, market_cap=5000)
    with (
        app.test_client() as client,
        patch(
            "routes.stocks.views.resolve_stocks_for_response",
            return_value={"us": [], "jp": []},
        ),
        patch(
            "routes.stocks.views.build_screener_base_rows_dispatch",
            return_value=[],
        ),
        patch(
            "routes.stocks.views.build_popular_symbol_items_dispatch",
            return_value=[("AAA", "Alpha", "us")],
        ),
        patch(
            "routes.stocks.views.build_screener_enrichment_dispatch",
            return_value={"AAA": aaa_row},
        ),
        patch(
            "routes.stocks.views.get_cached_dispatch",
            side_effect=lambda _key, fn, **_kw: fn(),
        ),
    ):
        resp = client.get("/api/screener")
        assert resp.status_code == 200
        assert resp.get_json()["missing"] == []


# ---------------------------------------------------------------------------
# D-05: dead market_is_open_cached removed
# ---------------------------------------------------------------------------


def test_d05_market_is_open_cached_removed():
    assert not hasattr(market_data_service, "market_is_open_cached")
    for path in list((REPO_ROOT / "services").rglob("*.py")) + list(
        (REPO_ROOT / "routes").rglob("*.py")
    ) + list((REPO_ROOT / "app_factory").rglob("*.py")) + [
        REPO_ROOT / "app.py",
        REPO_ROOT / "app_bg.py",
    ]:
        text = path.read_text(encoding="utf-8")
        assert "market_is_open_cached" not in text, path


# ---------------------------------------------------------------------------
# U12: iloc pre-slice is identical to post-conversion slice
# ---------------------------------------------------------------------------


def _ohlc_frame(n: int) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "Open": [float(i) + 0.1 for i in range(n)],
            "High": [float(i) + 0.2 for i in range(n)],
            "Low": [float(i) - 0.2 for i in range(n)],
            "Close": [float(i) for i in range(n)],
            "Volume": [1000 + i for i in range(n)],
            "MA5": [float(i) + 0.5 for i in range(n)],
            "MA25": [float(i) + 2.5 for i in range(n)],
        },
        index=dates,
    )
    df.index.name = "Date"
    return df


def test_u12_tail_slice_matches_post_conversion_slice():
    df = _ohlc_frame(400)
    chart, ohlc = _build_chart_ohlc_data(df, chart_data_limit=100, ohlc_data_limit=365)
    assert len(ohlc) == 365
    assert len(chart) == 100
    # Tail window is rows 35..399, not the head.
    assert ohlc[0]["c"] == 35.0
    assert ohlc[-1]["c"] == 399.0
    assert ohlc[0]["v"] == 1035
    # Chart window is the last 100 of the retained tail.
    assert chart[0]["price"] == 300.0
    assert chart[-1]["price"] == 399.0
    assert chart[0]["ma5"] == 300.5
    assert chart[0]["date"] == "01/01" or isinstance(chart[0]["date"], str)


def test_u12_short_and_empty_frames():
    chart, ohlc = _build_chart_ohlc_data(_ohlc_frame(10), chart_data_limit=100)
    assert len(ohlc) == 10
    assert len(chart) == 10
    assert ohlc[0]["c"] == 0.0

    empty = _ohlc_frame(0)
    chart_e, ohlc_e = _build_chart_ohlc_data(empty)
    assert chart_e == []
    assert ohlc_e == []
