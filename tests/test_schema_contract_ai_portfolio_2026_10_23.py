"""HTTP contract tests for canonical AI-portfolio schemas (U1/AI-PF slice).

``POST /api/ai-portfolio/generate|rebalance|save``,
``DELETE /api/ai-portfolio/custom`` and ``POST /api/ai-portfolio/copy-to-my``
now validate only through ``schemas.ai_portfolio`` (via route_helpers
``_parse_ai_*_request``). These tests pin the historic error envelopes so a
future schema edit cannot silently change an error code, message, or status.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app import app
from app_state import app_state
from error_codes import ErrorCode
from services.ai_portfolio_service import MAX_AI_PORTFOLIO_ITEMS


@pytest.fixture
def client():
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    with app.test_client() as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def reset_ai_portfolio_state():
    with app_state.market.user_stocks_lock:
        app_state.market.user_us.clear()
        app_state.market.user_jp.clear()
        app_state.market.user_idx.clear()
    yield
    with app_state.market.user_stocks_lock:
        app_state.market.user_us.clear()
        app_state.market.user_jp.clear()
        app_state.market.user_idx.clear()


def _post(client, path, payload=None, raw=None):
    data = raw if raw is not None else json.dumps(payload)
    return client.post(
        path,
        data=data,
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
    )


def _delete(client, path, payload=None, raw=None):
    data = raw if raw is not None else json.dumps(payload)
    return client.delete(
        path,
        data=data,
        content_type="application/json",
        environ_base={"REMOTE_ADDR": "127.0.0.1"},
        headers={"Origin": "http://localhost:5000"},
    )


def _error(resp):
    body = resp.get_json()
    assert body is not None
    assert set(body) == {
        "ok",
        "error",
        "error_flag",
        "code",
        "error_code",
        "message",
        "details",
    }
    assert body["ok"] is False
    assert body["error_flag"] is True
    assert body["code"] == str(body["error_code"])
    assert body["error"] == body["message"]
    return body


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def test_generate_missing_theme_is_missing(client):
    resp = _post(client, "/api/ai-portfolio/generate", {})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["theme"]}


def test_generate_blank_theme_is_missing(client):
    resp = _post(client, "/api/ai-portfolio/generate", {"theme": "   "})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["theme"]}


def test_generate_non_string_theme_keeps_japanese_message(client):
    resp = _post(client, "/api/ai-portfolio/generate", {"theme": 123})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": "themeは文字列で指定してください",
        "fields": ["theme"],
    }


def test_generate_over_long_theme_keeps_japanese_message(client):
    resp = _post(client, "/api/ai-portfolio/generate", {"theme": "x" * 121})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {"reason": "themeは120文字以内で指定してください"}


def test_generate_malformed_json_is_rejected(client):
    resp = _post(client, "/api/ai-portfolio/generate", raw="{not json")
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT


# ---------------------------------------------------------------------------
# rebalance
# ---------------------------------------------------------------------------


def test_rebalance_non_string_theme_keeps_japanese_message(client):
    resp = _post(client, "/api/ai-portfolio/rebalance", {"theme": 123})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": "themeは文字列で指定してください",
        "fields": ["theme"],
    }


def test_rebalance_over_long_theme_is_malformed(client):
    resp = _post(client, "/api/ai-portfolio/rebalance", {"theme": "x" * 121})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT
    assert body["details"] == {"reason": "テーマは120文字以内で入力してください"}


# ---------------------------------------------------------------------------
# delete custom
# ---------------------------------------------------------------------------


def test_delete_missing_id_is_missing(client):
    resp = _delete(client, "/api/ai-portfolio/custom", {})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["id"]}


def test_delete_blank_id_is_missing(client):
    resp = _delete(client, "/api/ai-portfolio/custom", {"id": "   "})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MISSING_REQUIRED_FIELD
    assert body["details"] == {"fields": ["id"]}


def test_delete_non_string_id_keeps_japanese_message(client):
    resp = _delete(client, "/api/ai-portfolio/custom", {"id": 123})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": "idは文字列で指定してください",
        "fields": ["id"],
    }


def test_delete_over_long_id_is_malformed(client):
    resp = _delete(client, "/api/ai-portfolio/custom", {"id": "x" * 257})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT
    assert body["details"] == {"reason": "IDは256文字以内で指定してください"}


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


def test_save_missing_portfolio_is_malformed(client):
    resp = _post(client, "/api/ai-portfolio/save", {"theme": "tech"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT
    assert body["details"] == {"reason": "無効なポートフォリオデータです"}


def test_save_non_dict_portfolio_is_malformed(client):
    resp = _post(
        client, "/api/ai-portfolio/save", {"portfolio": "not-a-dict"}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT
    assert body["details"] == {"reason": "無効なポートフォリオデータです"}


def test_save_blank_theme_is_malformed(client):
    resp = _post(
        client,
        "/api/ai-portfolio/save",
        {"theme": "   ", "portfolio": {"title": "t", "items": []}},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT
    assert body["details"] == {"reason": "無効なポートフォリオデータです"}


# ---------------------------------------------------------------------------
# copy-to-my
# ---------------------------------------------------------------------------


def _item(symbol="NVDA", market="us", weight_pct=30.0, target_price=160.0):
    return {
        "symbol": symbol,
        "market": market,
        "weight_pct": weight_pct,
        "target_price": target_price,
    }


def test_copy_missing_items_is_malformed(client):
    resp = _post(client, "/api/ai-portfolio/copy-to-my", {})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT
    assert body["details"] == {"reason": "itemsリストが必要です"}


def test_copy_non_list_items_is_malformed(client):
    resp = _post(client, "/api/ai-portfolio/copy-to-my", {"items": "x"})
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT
    assert body["details"] == {"reason": "itemsリストが必要です"}


def test_copy_too_many_items_keeps_limit_message(client):
    payload = {
        "items": [
            {"symbol": f"SYM{i:02d}", "market": "us", "weight_pct": 1.0, "target_price": 10.0}
            for i in range(MAX_AI_PORTFOLIO_ITEMS + 1)
        ]
    }
    resp = _post(client, "/api/ai-portfolio/copy-to-my", payload)
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": f"items は最大 {MAX_AI_PORTFOLIO_ITEMS} 件まで指定できます"
    }


def test_copy_non_object_item_reports_index(client):
    resp = _post(
        client, "/api/ai-portfolio/copy-to-my", {"items": [_item(), "bad"]}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.MALFORMED_INPUT
    assert body["details"] == {"reason": "items[1] が無効です"}


def test_copy_bad_symbol_reports_index(client):
    resp = _post(
        client, "/api/ai-portfolio/copy-to-my", {"items": [_item(symbol="AAPL!")]}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {"reason": "items[0] のシンボルまたは市場が無効です"}


def test_copy_bad_market_reports_index(client):
    resp = _post(
        client, "/api/ai-portfolio/copy-to-my", {"items": [_item(market="eu")]}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {"reason": "items[0] のシンボルまたは市場が無効です"}


def test_copy_jp_symbol_in_us_market_reports_mismatch(client):
    resp = _post(
        client, "/api/ai-portfolio/copy-to-my", {"items": [_item(symbol="7203.T")]}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_MARKET
    assert body["details"]["reason"].startswith("items[0]: ")


def test_copy_duplicate_reports_index_and_pair(client):
    payload = {"items": [_item(symbol="NVDA"), _item(symbol="nvda")]}
    resp = _post(client, "/api/ai-portfolio/copy-to-my", payload)
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": "items[1] の銘柄が重複しています: NVDA (us)"
    }


def test_copy_bool_numeric_reports_index(client):
    resp = _post(
        client,
        "/api/ai-portfolio/copy-to-my",
        {"items": [_item(weight_pct=True)]},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {"reason": "items[0] の数値が無効です（真偽値は不可）"}


def test_copy_bad_numeric_reports_index(client):
    resp = _post(
        client,
        "/api/ai-portfolio/copy-to-my",
        {"items": [_item(weight_pct="bad")]},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {"reason": "items[0] の数値が無効です"}


def test_copy_zero_weight_reports_range(client):
    resp = _post(
        client, "/api/ai-portfolio/copy-to-my", {"items": [_item(weight_pct=0.0)]}
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": "items[0] の weight_pct は 0〜100 の範囲が必要です"
    }


def test_copy_zero_target_reports_range(client):
    resp = _post(
        client,
        "/api/ai-portfolio/copy-to-my",
        {"items": [_item(target_price=0.0)]},
    )
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"]["reason"].startswith("items[0] の target_price は")


def test_copy_over_total_weight_reports_total(client):
    payload = {
        "items": [
            _item(symbol="AAA", weight_pct=60.0),
            _item(symbol="BBB", weight_pct=41.0),
        ]
    }
    resp = _post(client, "/api/ai-portfolio/copy-to-my", payload)
    assert resp.status_code == 400
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT
    assert body["details"] == {
        "reason": "items の weight_pct 合計は100以下である必要があります"
    }


def test_copy_second_item_error_beats_first_item_ok(client):
    # Sequential validation: the first failing index wins.
    payload = {"items": [_item(symbol="NVDA"), _item(symbol="BAD!")]}
    resp = _post(client, "/api/ai-portfolio/copy-to-my", payload)
    assert resp.status_code == 400
    body = _error(resp)
    assert body["details"] == {"reason": "items[1] のシンボルまたは市場が無効です"}


def test_copy_huge_int_weight_is_client_error_not_500(client):
    payload = {"items": [_item(weight_pct=10**1000)]}
    resp = _post(client, "/api/ai-portfolio/copy-to-my", payload)
    # Must be a 4xx contract error, never an unhandled 500 from OverflowError.
    assert 400 <= resp.status_code < 500
    body = _error(resp)
    assert body["error_code"] == ErrorCode.INVALID_INPUT


def test_copy_valid_items_normalize_and_persist(client):
    with patch("utils.storage.save_user_stocks", return_value=None):
        resp = _post(
            client,
            "/api/ai-portfolio/copy-to-my",
            {"items": [_item(symbol="  nvda  ", weight_pct="30")]}
            ,
        )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["added_count"] == 1
    with app_state.market.user_stocks_lock:
        assert "NVDA" in app_state.market.user_us


def test_copy_missing_target_defaults_to_100(client):
    with patch("utils.storage.save_user_stocks", return_value=None):
        resp = _post(
            client,
            "/api/ai-portfolio/copy-to-my",
            {"items": [{"symbol": "NVDA", "market": "us", "weight_pct": 30.0}]},
        )
    assert resp.status_code == 200
    with app_state.market.user_stocks_lock:
        assert app_state.market.user_us["NVDA"]["avg_price"] == 100.0


def test_copy_schema_normalizes_sequentially():
    from schemas.ai_portfolio import AIPortfolioCopyToMyRequest

    req = AIPortfolioCopyToMyRequest.model_validate(
        {"items": [{"symbol": "7203", "market": "jp", "weight_pct": 10.0}]}
    )
    assert req.items[0].symbol == "7203.T"
    assert req.items[0].market == "jp"
    assert req.items[0].target_price == 100.0
