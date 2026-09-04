"""Regression tests for code review findings R1 to R8."""

import json
import types
from unittest.mock import MagicMock, patch

import config_store
import credential_manager
import crypto_utils
from error_codes import ErrorCode
from error_handlers import register_error_handlers
from services.realtime_engine import RealtimeMarketEngine, _tv_purge_key_variants
from services.search.langsearch import langsearch_rerank
from services.search_service import _get_market_trending_titles, _market_trends_cache_key
from utils.caching import _set_cached_value
from utils.stock_payload import _extract_portfolio_fields
from utils.validators import (
    AppConfigSchema,
    safe_parse_analysis_result,
)


class TestReviewAutonomousHeadFixes:
    """Test suite verifying root-cause fixes for findings R1 - R8."""

    def test_r1_save_api_credentials_keyring_read_error_falls_back_gracefully(
        self, tmp_path, monkeypatch
    ):
        """R1: save_api_credentials should not crash when keyring get_password raises KeyringError."""
        runtime_config = tmp_path / "config.json"
        runtime_config.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(config_store, "CONFIG_FILE", runtime_config)
        monkeypatch.setenv("MNS_EPHEMERAL_FALLBACK", "1")

        mock_kr = MagicMock()
        mock_kr.get_password.side_effect = RuntimeError("Keyring daemon offline")
        # set_password also fails to trigger ephemeral fallback in _encode_secret
        from crypto_utils import KeyringError

        mock_kr.set_password.side_effect = KeyringError("Keyring unavailable")

        with (
            patch.object(credential_manager, "_keyring_available", return_value=True),
            patch.object(credential_manager, "_keyring", return_value=mock_kr),
            patch.object(crypto_utils, "KEYRING_AVAILABLE", True),
            patch.object(crypto_utils, "keyring", mock_kr),
            patch.object(crypto_utils, "_is_windows", return_value=False),
        ):
            # This should not raise RuntimeError("Unable to inspect existing secure credential state")
            # and should successfully save via ephemeral fallback
            with patch.object(credential_manager.logger, "debug") as mock_debug:
                credential_manager.save_api_credentials(
                    mistral_api_key="sk-test-12345678901234567890123456789012"
                )
            logged = " ".join(str(call) for call in mock_debug.call_args_list)
            assert "RuntimeError" in logged
            assert "Keyring daemon offline" not in logged

        saved = json.loads(runtime_config.read_text(encoding="utf-8"))
        creds = saved.get("api_credentials", {})
        self_mistral = creds.get("mistral_api_key")
        assert self_mistral is not None
        assert self_mistral.get("scheme") == "ephemeral"

    def test_r2_tv_purge_key_variants_bidirectional_dash_dot_aliases(self):
        """R2: _tv_purge_key_variants generates both dot and dash aliases for class shares."""
        # When unregistering BRK.B
        variants_dotted = set(_tv_purge_key_variants("BRK.B"))
        assert "BRK.B" in variants_dotted
        assert "BRK-B" in variants_dotted
        assert "NYSE:BRK.B" in variants_dotted
        assert "NYSE:BRK-B" in variants_dotted

        # When unregistering BRK-B
        variants_dashed = set(_tv_purge_key_variants("BRK-B"))
        assert "BRK-B" in variants_dashed
        assert "BRK.B" in variants_dashed

        # Verify engine unregister purges stranded dash keys
        engine = RealtimeMarketEngine()
        engine.market_store["NYSE:BRK.B"] = {"price": 450.0}
        engine.market_store["BRK.B"] = {"price": 450.0}
        engine.market_store["BRK-B"] = {"price": 450.0}

        engine.unregister_symbol("BRK.B", "us")
        assert "BRK.B" not in engine.market_store
        assert "BRK-B" not in engine.market_store
        assert "NYSE:BRK.B" not in engine.market_store

    def test_r3_trending_titles_cache_empty_list_prevents_synchronous_rebuild(self):
        """R3: Cached empty list [] is treated as cache hit, preventing synchronous search block."""
        cache_key = _market_trends_cache_key("us", "ddgs_only")
        _set_cached_value(cache_key, [], duration=300)

        with patch("services.search_service._build_market_trending_titles") as mock_build:
            res = _get_market_trending_titles("us", "ddgs_only", "dummy_lang_key")
            assert res == []
            # mock_build must NOT be called synchronously
            mock_build.assert_not_called()

    def test_r4_langsearch_rerank_bounds_and_type_validation(self):
        """R4: langsearch_rerank correctly ignores negative, non-int, or out-of-bound indices."""
        docs = [
            {"title": "Doc 0", "snippet": "First doc"},
            {"title": "Doc 1", "snippet": "Second doc"},
        ]

        # Case 1: Negative index should NOT access docs[-1]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [
                {"index": -1, "relevance_score": 0.99},
                {"index": 0, "relevance_score": 0.80},
                {"index": "1", "relevance_score": 0.90},  # string index
                {"index": True, "relevance_score": 0.95},  # bool index
                {"index": 999, "relevance_score": 0.99},  # out of bound
            ]
        }

        with patch(
            "services.search.langsearch._langsearch_post_json",
            return_value=mock_resp.json.return_value,
        ):
            reranked = langsearch_rerank("test query", docs, "dummy_api_key")
            assert len(reranked) == 1
            assert reranked[0]["title"] == "Doc 0"
            assert reranked[0]["relevance_score"] == 0.80

    def test_r4_langsearch_rerank_ignores_malformed_result_entries(self):
        docs = [{"title": "Doc 0"}, {"title": "Doc 1"}]
        malformed_response = {"results": [None, {"index": 0, "relevance_score": 0.80}]}

        with patch(
            "services.search.langsearch._langsearch_post_json",
            return_value=malformed_response,
        ):
            reranked = langsearch_rerank("test query", docs, "dummy_api_key")

        assert reranked == [{"title": "Doc 0", "relevance_score": 0.80}]

    def test_r5_safe_parse_analysis_result_handles_sdk_objects(self):
        """R5: safe_parse_analysis_result parses non-dict SDK response objects seamlessly."""
        json_content = json.dumps(
            {
                "analysis_summary": "Strong growth in Cloud revenue",
                "recommendation": "買い",
                "sentiment": "強気",
                "target_price_3m": 250.0,
                "upside_3m": "+15%",
                "confidence": "高",
            }
        )

        # Simulate Mistral SDK object
        msg_obj = types.SimpleNamespace(content=json_content)
        choice_obj = types.SimpleNamespace(message=msg_obj)
        resp_obj = types.SimpleNamespace(choices=[choice_obj])

        parsed = safe_parse_analysis_result(resp_obj, "test_api_key")
        assert parsed["fallback_used"] is False
        assert parsed["recommendation"] == "買い"
        assert parsed["sentiment"] == "強気"
        assert parsed["target_price_3m"] == 250.0

    def test_r6_extract_portfolio_fields_sanitizes_non_finite_floats(self):
        """R6: _extract_portfolio_fields converts inf/nan to sanitized 0.0 or None."""
        name, shares, avg_price, fx = _extract_portfolio_fields(
            {
                "name": "TEST",
                "shares": "inf",
                "avg_price": "nan",
                "avg_fx_rate": "-inf",
            }
        )
        assert name == "TEST"
        assert shares == 0.0
        assert avg_price == 0.0
        assert fx is None

        # Also test negative shares/price
        name, shares, avg_price, fx = _extract_portfolio_fields(
            {
                "name": "TEST2",
                "shares": -50.0,
                "avg_price": -100.0,
                "avg_fx_rate": -1.5,
            }
        )
        assert shares == 0.0
        assert avg_price == 0.0
        assert fx is None

    def test_r7_app_config_schema_preserves_ephemeral_fields(self):
        """R7: AppConfigSchema validates and preserves ephemeral credential warning flags."""
        raw_state = {
            "has_mistral_api_key": True,
            "has_langsearch_api_key": False,
            "has_tavily_api_key": False,
            "has_alphavantage_api_key": False,
            "mistral_model": "mistral-large-latest",
            "is_ai_technical_lines_eligible": True,
            "credentials_ephemeral": True,
            "credentials_ephemeral_keys": ["mistral_api_key"],
            "credentials_ephemeral_warning": "Ephemeral warning text",
        }

        validated = AppConfigSchema.model_validate(raw_state).model_dump()
        assert validated["credentials_ephemeral"] is True
        assert validated["credentials_ephemeral_keys"] == ["mistral_api_key"]
        assert validated["credentials_ephemeral_warning"] == "Ephemeral warning text"

    def test_r8_bad_request_error_handler_clean_reason(self):
        """R8: 400 Bad Request error handler returns clean description without HTTP prefix."""
        from flask import Flask
        from werkzeug.exceptions import BadRequest

        app = Flask("test_r8")
        register_error_handlers(app)

        with app.test_request_context():
            err = BadRequest("Invalid stock ticker format")
            resp, status = app.error_handler_spec[None][400][BadRequest](err)
            assert status == 400
            data = resp.get_json()
            assert data["ok"] is False
            assert data["error_code"] == ErrorCode.BAD_REQUEST.value
            assert data["details"]["reason"] == "Invalid stock ticker format"

    def test_rotate_corrupt_backups_safe_stat(self, tmp_path):
        """Verify _rotate_corrupt_backups handles missing/unlinked files during rotation without error."""
        for i in range(8):
            f = tmp_path / f"config.json.corrupt.{i}.bak"
            f.write_text("{}", encoding="utf-8")

        config_store._rotate_corrupt_backups(tmp_path, limit=3)
        remaining = list(tmp_path.glob("config.json.corrupt.*.bak"))
        assert len(remaining) == 3
