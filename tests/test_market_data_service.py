"""Tests for services/market_data_service.py."""

import unittest
from unittest.mock import MagicMock, patch

from app_state import app_state
from services.market_data_service import (
    build_heatmap_payload,
    build_screener_base_rows,
    build_screener_enrichment,
)


class MarketDataServiceTestCase(unittest.TestCase):
    def setUp(self):
        app_state.payload_disk_cache.clear()

    def tearDown(self):
        app_state.payload_disk_cache.clear()

    @patch("services.market_data_service.fetch_stocks_batch")
    def test_build_heatmap_payload_orders_and_filters_rows(self, mock_fetch_stocks_batch):
        mock_fetch_stocks_batch.return_value = [
            {
                "symbol": "AAA",
                "name": "Alpha",
                "price": 10.0,
                "volume": 100,
                "sharesOutstanding": 50,
                "sector": "Tech",
                "change_percent": 1.5,
            },
            {
                "symbol": "BBB",
                "name": "Beta",
                "price": 20.0,
                "market_cap": 5000,
                "sector": "Finance",
                "change_percent": -2.0,
            },
            {
                "symbol": "CCC",
                "name": "Gamma",
                "price": 0.0,
                "market_cap": 0,
                "sector": "Other",
            },
        ]

        payload = build_heatmap_payload("us", ["AAA", "BBB", "CCC"])

        self.assertIn("stocks", payload)
        self.assertEqual([row["symbol"] for row in payload["stocks"]], ["BBB", "AAA"])
        self.assertGreater(payload["stocks"][0]["market_cap"], payload["stocks"][1]["market_cap"])

    def test_build_screener_base_rows_normalizes_snapshot_rows(self):
        stocks_data = {
            "us": [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "price": 100.0,
                    "change_percent": 2.5,
                    "market_cap": 123456,
                    "volume": 1000,
                    "high": 101.0,
                    "low": 99.0,
                    "sector": "Tech",
                }
            ],
            "jp": [],
        }

        rows = build_screener_base_rows(stocks_data, "all")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "AAPL")
        self.assertEqual(rows[0]["market"], "us")
        self.assertEqual(rows[0]["price"], 100.0)
        self.assertEqual(rows[0]["change_percent"], 2.5)
        self.assertEqual(rows[0]["sector"], "Tech")

    @patch("services.market_data_service.fetch_stocks_batch")
    @patch("services.market_data_service.get_stock_info_cached")
    def test_build_screener_enrichment_prefers_disk_cache_then_fallback(self, mock_get_info, mock_fetch):
        app_state.payload_disk_cache.set(
            "payload_AAA_us",
            {
                "symbol": "AAA",
                "name": "Cached Alpha",
                "price": 11.0,
                "change_percent": 1.1,
                "market_cap": 1100,
                "volume": 10,
                "high": 12.0,
                "low": 10.0,
                "sector": "Tech",
            },
        )
        mock_fetch.return_value = [
            {
                "symbol": "BBB",
                "name": "Batch Beta",
                "price": 20.0,
                "change_percent": -2.0,
                "market_cap": 2000,
                "volume": 50,
                "high": 21.0,
                "low": 19.0,
                "sector": "Finance",
            }
        ]
        mock_get_info.return_value = {}

        rows = build_screener_enrichment([("AAA", "Alpha", "us"), ("BBB", "Beta", "us")], None)

        self.assertEqual(rows["AAA"]["name"], "Cached Alpha")
        self.assertEqual(rows["BBB"]["name"], "Batch Beta")
        mock_get_info.assert_not_called()
