"""Unit and integration tests for AI Portfolio feature (presets, Web Search + Mistral generation, storage, and API endpoints)."""

import json
from unittest.mock import patch

from services.ai_portfolio_service import (
    DEFAULT_PRESET_CONFIGS,
    delete_custom_ai_portfolio,
    generate_ai_portfolio_by_theme,
    load_saved_ai_portfolios,
    save_custom_ai_portfolio,
)
from utils.validators import AiPortfolioItemSchema, AiPortfolioResponseSchema


def test_ai_portfolio_pydantic_schemas():
    item = AiPortfolioItemSchema(
        symbol="NVDA",
        market="us",
        weight_pct=30.0,
        target_price=160.0,
        rationale="AI leader",
        risk_level="high",
    )
    assert item.symbol == "NVDA"
    assert item.weight_pct == 30.0

    response = AiPortfolioResponseSchema(
        title="Tech Portfolio",
        description="Tech growth strategy",
        risk_level="高リスク",
        expected_return="20%",
        commentary="Solid AI strategy",
        items=[item],
    )
    assert len(response.items) == 1
    assert response.title == "Tech Portfolio"


def test_default_presets_structure():
    assert "tech" in DEFAULT_PRESET_CONFIGS
    assert "dividend" in DEFAULT_PRESET_CONFIGS
    assert "balanced" in DEFAULT_PRESET_CONFIGS

    tech = DEFAULT_PRESET_CONFIGS["tech"]
    assert tech["id"] == "tech"
    assert "theme" in tech


def test_generate_ai_portfolio_by_preset():
    with patch("services.ai_portfolio_service.get_mistral_api_key", return_value=""):
        res = generate_ai_portfolio_by_theme("tech")
        assert res["id"] == "tech"
        assert len(res["items"]) >= 3

        res_div = generate_ai_portfolio_by_theme("dividend")
        assert res_div["id"] == "dividend"


def test_generate_ai_portfolio_fallback_custom_theme():
    with patch("services.ai_portfolio_service.get_mistral_api_key", return_value=""):
        res = generate_ai_portfolio_by_theme("サイバーセキュリティ")
        assert "サイバーセキュリティ" in res["title"] or "サイバーセキュリティ" in res["commentary"]
        assert len(res["items"]) > 0


def test_generate_ai_portfolio_marks_generation_source(tmp_path):
    """Fallback and AI-generated portfolios must be distinguishable (R4)."""
    test_storage = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_storage), \
         patch("services.ai_portfolio_service.get_mistral_api_key", return_value=""):
        res = generate_ai_portfolio_by_theme("tech")
        assert res.get("generated_by") == "fallback"
        saved = load_saved_ai_portfolios()
        assert saved and saved[0].get("generated_by") == "fallback"

    # Valid AI response → marked as AI-generated and persisted.
    mock_mistral_resp = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "title": "AI生成ポートフォリオ",
                        "description": "D",
                        "risk_level": "中リスク",
                        "expected_return": "10%",
                        "commentary": "C",
                        "items": [
                            {"symbol": "NVDA", "market": "us", "weight_pct": 100.0, "target_price": 160.0, "rationale": "r", "risk_level": "mid"}
                        ],
                    })
                }
            }
        ]
    }
    test_storage2 = tmp_path / "ai_portfolios_ai.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_storage2), \
         patch("services.ai_portfolio_service.get_mistral_api_key", return_value="mock_key"), \
         patch("services.ai_portfolio_service.collect_symbol_research_context", return_value=""), \
         patch("services.ai_portfolio_service.call_mistral_chat", return_value=mock_mistral_resp):
        res2 = generate_ai_portfolio_by_theme("クリーンエネルギー", force_rebalance=True)
        assert res2.get("generated_by") == "ai"
        saved2 = load_saved_ai_portfolios()
        assert saved2 and saved2[0].get("generated_by") == "ai"


def test_generate_ai_portfolio_parse_failure_falls_back(tmp_path):
    """Unparseable LLM output must fall back to a marked heuristic portfolio (R4)."""
    test_storage = tmp_path / "ai_portfolios_broken.json"
    mock_mistral_resp = {"choices": [{"message": {"content": "not json at all {{{ "}}]}
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_storage), \
         patch("services.ai_portfolio_service.get_mistral_api_key", return_value="mock_key"), \
         patch("services.ai_portfolio_service.collect_symbol_research_context", return_value=""), \
         patch("services.ai_portfolio_service.call_mistral_chat", return_value=mock_mistral_resp):
        res = generate_ai_portfolio_by_theme("tech", force_rebalance=True)
        assert res.get("generated_by") == "fallback"
        assert len(res["items"]) > 0


def test_generate_ai_portfolio_mistral_api_with_web_search():
    mock_mistral_resp = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "title": "🤖 クリーンエネルギーAIポートフォリオ",
                        "description": "脱炭素・次世代エナジー戦略",
                        "risk_level": "中リスク",
                        "expected_return": "12-15%",
                        "commentary": "再エネ需要増加に伴う有望企業を厳選",
                        "items": [
                            {"symbol": "NEE", "market": "us", "weight_pct": 50.0, "target_price": 90.0, "rationale": "再エネ大手", "risk_level": "mid"},
                            {"symbol": "TSLA", "market": "us", "weight_pct": 50.0, "target_price": 280.0, "rationale": "EV・蓄電池リーダー", "risk_level": "high"}
                        ]
                    })
                }
            }
        ]
    }
    with patch("services.ai_portfolio_service.get_mistral_api_key", return_value="mock_key"), \
         patch("services.ai_portfolio_service.collect_symbol_research_context", return_value="[Search Results: NEE, TSLA]"), \
         patch("services.ai_portfolio_service.call_mistral_chat", return_value=mock_mistral_resp) as mock_chat:
        res = generate_ai_portfolio_by_theme("クリーンエネルギー", force_rebalance=True)
        assert res["title"] == "🤖 クリーンエネルギーAIポートフォリオ"
        assert len(res["items"]) == 2
        # Verify Web Search context was injected into prompt
        mock_chat.assert_called_once()
        call_args = mock_chat.call_args[1]
        assert "<web_search_research_context>" in call_args["messages"][1]["content"]


def test_save_and_delete_custom_ai_portfolio(tmp_path):
    test_storage = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_storage):
        sample_portfolio = {
            "id": "custom-test-123",
            "title": "テストテーマ",
            "items": [{"symbol": "AAPL", "market": "us", "weight_pct": 100.0, "rationale": "Test"}]
        }
        assert save_custom_ai_portfolio(sample_portfolio) is True
        saved = load_saved_ai_portfolios()
        assert len(saved) == 1
        assert saved[0]["id"] == "custom-test-123"

        assert delete_custom_ai_portfolio("custom-test-123") is True
        assert len(load_saved_ai_portfolios()) == 0


def test_save_ai_portfolio_sanitizes_payload(tmp_path):
    """Saved payloads must be scrubbed of HTML and malformed items (R2)."""
    test_storage = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_storage):
        portfolio = {
            "id": "xss-1",
            "title": "<img src=x onerror=alert(1)>タイトル",
            "commentary": "<script>alert(1)</script>解説",
            "items": [
                {"symbol": "AAPL", "market": "us", "weight_pct": 50.0, "target_price": 250.0, "rationale": "<b>値上がり</b>見込み", "risk_level": "mid"},
                {"symbol": "<img src=x>", "market": "us", "weight_pct": 10.0},
                {"symbol": "NVDA", "market": "us", "weight_pct": 999.0},
                {"symbol": "7203.T", "market": "jp", "weight_pct": "not-a-number"},
                "not-a-dict",
            ],
        }
        assert save_custom_ai_portfolio(portfolio) is True
        stored = load_saved_ai_portfolios()[0]
        assert "<img" not in stored["title"]
        assert "<script" not in stored["commentary"]
        assert len(stored["items"]) == 1
        assert stored["items"][0]["symbol"] == "AAPL"
        assert stored["items"][0]["rationale"] == "値上がり見込み"
        assert stored["items"][0]["target_price"] == 250.0


def test_save_ai_portfolio_enforces_max_cap(tmp_path):
    """The JSON database must stay bounded (R5)."""
    from services.ai_portfolio_service import MAX_SAVED_AI_PORTFOLIOS

    test_storage = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_storage):
        for i in range(MAX_SAVED_AI_PORTFOLIOS + 5):
            assert save_custom_ai_portfolio({"id": f"cap-{i}", "title": f"T{i}", "theme": f"theme-{i}"}) is True
        saved = load_saved_ai_portfolios()
        assert len(saved) == MAX_SAVED_AI_PORTFOLIOS


def test_delete_custom_ai_portfolio_missing_returns_false(tmp_path):
    """Deleting a non-existent portfolio must return False, not raise (R5)."""
    test_storage = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_storage):
        assert delete_custom_ai_portfolio("does-not-exist") is False


def test_api_copy_to_my_rejects_invalid_items_without_mutation():
    """A single malformed item must return 400 and leave state untouched (R3)."""
    from app import app
    from app_state import app_state

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
            client = app.test_client()
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = {"AAPL": "Apple"}
                app_state.market.user_jp = {}

            # Non-numeric weight_pct on the second item: nothing may be applied.
            res = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {"symbol": "NVDA", "market": "us", "weight_pct": 30.0, "target_price": 160.0},
                        {"symbol": "MSFT", "market": "us", "weight_pct": "bad", "target_price": 200.0},
                    ]
                },
            )
            assert res.status_code == 400
            with app_state.market.user_stocks_lock:
                assert app_state.market.user_us == {"AAPL": "Apple"}

            # Invalid symbol / market must also be rejected up front.
            res2 = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={"items": [{"symbol": "<img>", "market": "us", "weight_pct": 10.0, "target_price": 100.0}]},
            )
            assert res2.status_code == 400
            res3 = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={"items": [{"symbol": "NVDA", "market": "eu", "weight_pct": 10.0, "target_price": 100.0}]},
            )
            assert res3.status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_api_copy_to_my_skips_existing_holdings():
    """Existing holdings must never be overwritten by AI-simulated values (R3)."""
    from app import app
    from app_state import app_state

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
            client = app.test_client()
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = {"NVDA": {"name": "NVIDIA", "shares": 100.0, "avg_price": 50.0}}
                app_state.market.user_jp = {}

            res = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {"symbol": "NVDA", "market": "us", "weight_pct": 30.0, "target_price": 160.0},
                        {"symbol": "MSFT", "market": "us", "weight_pct": 30.0, "target_price": 200.0},
                    ]
                },
            )
            assert res.status_code == 200
            data = res.get_json()
            assert data["added_count"] == 1
            assert data["skipped"] == ["NVDA (us)"]
            with app_state.market.user_stocks_lock:
                assert app_state.market.user_us["NVDA"]["shares"] == 100.0
                assert app_state.market.user_us["NVDA"]["avg_price"] == 50.0
                assert "MSFT" in app_state.market.user_us
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_api_copy_to_my_defaults_missing_target_price():
    """Items without a target price must fall back to 100.0 instead of 400 (R3)."""
    from app import app
    from app_state import app_state

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
            client = app.test_client()
            res = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {"symbol": "NVDA", "market": "us", "weight_pct": 30.0, "target_price": None}
                    ]
                },
            )
            assert res.status_code == 200
            assert res.get_json()["added_count"] == 1
            with app_state.market.user_stocks_lock:
                holding = app_state.market.user_us["NVDA"]
                assert holding["avg_price"] == 100.0
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_api_ai_portfolio_endpoints():
    from app import app

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)), \
             patch("services.ai_portfolio_service.get_mistral_api_key", return_value=""):
            client = app.test_client()
            # GET /api/ai-portfolio
            res = client.get("/api/ai-portfolio")
            assert res.status_code == 200
            data = res.get_json()
            assert data["ok"] is True
            assert "presets" in data

            # POST /api/ai-portfolio/generate
            gen_res = client.post("/api/ai-portfolio/generate", json={"theme": "tech"})
            assert gen_res.status_code == 200
            gen_data = gen_res.get_json()
            assert gen_data["ok"] is True
            assert gen_data["portfolio"]["id"] == "tech"

            # POST /api/ai-portfolio/rebalance
            reb_res = client.post("/api/ai-portfolio/rebalance", json={"theme": "tech"})
            assert reb_res.status_code == 200
            reb_data = reb_res.get_json()
            assert reb_data["ok"] is True

            # POST /api/ai-portfolio/copy-to-my
            copy_res = client.post("/api/ai-portfolio/copy-to-my", json={
                "items": [
                    {"symbol": "NVDA", "market": "us", "weight_pct": 30.0, "target_price": 160.0}
                ]
            })
            assert copy_res.status_code == 200
            copy_data = copy_res.get_json()
            assert copy_data["ok"] is True
            assert copy_data["added_count"] >= 1
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_api_copy_to_my_triggers_realtime_and_market_sync():
    """copy-to-my must trigger realtime symbol sync, announce market state, and schedule sync (R1)."""
    from app import app

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)), \
             patch("routes.api_stocks._sync_realtime_symbol") as mock_realtime_sync, \
             patch("app_bg.announce_current_market_state") as mock_announce, \
             patch("routes.api_stocks.schedule_sync_all_stocks_now") as mock_schedule_sync:
            client = app.test_client()
            res = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {"symbol": "AMD", "market": "us", "weight_pct": 50.0, "target_price": 150.0}
                    ]
                },
            )
            assert res.status_code == 200
            mock_realtime_sync.assert_called_once_with("AMD", "us", register=True)
            mock_announce.assert_called_once()
            mock_schedule_sync.assert_called_once()
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_save_custom_ai_portfolio_same_theme_different_id_does_not_overwrite(tmp_path):
    """Portfolios with identical themes but different IDs must not overwrite each other (R3)."""
    test_storage = tmp_path / "ai_portfolios.json"
    with patch("services.ai_portfolio_service.AI_PORTFOLIO_STORAGE_FILE", test_storage):
        p1 = {"id": "custom-1", "title": "Portfolio 1", "theme": "AI・半導体", "items": []}
        p2 = {"id": "custom-2", "title": "Portfolio 2", "theme": "AI・半導体", "items": []}
        assert save_custom_ai_portfolio(p1) is True
        assert save_custom_ai_portfolio(p2) is True
        saved = load_saved_ai_portfolios()
        assert len(saved) == 2
        ids = [p["id"] for p in saved]
        assert "custom-1" in ids
        assert "custom-2" in ids


def test_copy_ai_portfolio_us_shares_fx_conversion():
    """Verify US stock shares calculation in copy-to-my converts allocated JPY value to USD first (R1)."""
    from app import app
    from app_state import app_state

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)), \
             patch("routes.api_stocks.save_user_stocks"), \
             patch("routes.api_stocks._sync_realtime_symbol"), \
             patch("app_bg.announce_current_market_state"), \
             patch("routes.api_stocks.schedule_sync_all_stocks_now"):
            client = app.test_client()
            app_state.market.last_usdjpy_rate = 150.0

            # Clean user_us container for test isolation
            with app_state.market.user_stocks_lock:
                app_state.market.user_us.pop("TESTUS", None)

            # 20% weight of 10,000,000 JPY = 2,000,000 JPY.
            # At 150 JPY/USD, 2,000,000 JPY = $13,333.33 USD.
            # At target_price = $100.0 USD, shares should be ~133.33 (not 20,000!).
            res = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {"symbol": "TESTUS", "market": "us", "weight_pct": 20.0, "target_price": 100.0}
                    ]
                },
            )
            assert res.status_code == 200
            assert res.get_json()["ok"] is True

            with app_state.market.user_stocks_lock:
                item = app_state.market.user_us.get("TESTUS")
                assert item is not None
                assert item["shares"] == 133.33
                app_state.market.user_us.pop("TESTUS", None)
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_copy_ai_portfolio_updates_sse_cache_immediately():
    """copy-to-my must update shares and avg_price in sse caches immediately (R1)."""
    from app import app
    from app_state import app_state

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)), \
             patch("routes.api_stocks.save_user_stocks"), \
             patch("routes.api_stocks._sync_realtime_symbol"), \
             patch("app_bg.announce_current_market_state"), \
             patch("routes.api_stocks.schedule_sync_all_stocks_now"):
            client = app.test_client()

            with app_state.market.user_stocks_lock:
                app_state.market.user_us.pop("TESTSSE", None)

            with app_state.cache.sse_data_lock:
                app_state.market.current_stocks_cache["us"] = [
                    {"symbol": "TESTSSE", "name": "TESTSSE", "price": 100.0}
                ]
                app_state.market.target_stocks_cache["us"] = [
                    {"symbol": "TESTSSE", "name": "TESTSSE", "price": 100.0}
                ]

            res = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {"symbol": "TESTSSE", "market": "us", "weight_pct": 15.0, "target_price": 100.0}
                    ]
                },
            )
            assert res.status_code == 200

            with app_state.cache.sse_data_lock:
                cur = next((s for s in app_state.market.current_stocks_cache.get("us", []) if s.get("symbol") == "TESTSSE"), None)
                assert cur is not None
                assert "shares" in cur
                assert cur["avg_price"] == 100.0

            with app_state.market.user_stocks_lock:
                app_state.market.user_us.pop("TESTSSE", None)
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


def test_copy_ai_portfolio_validates_max_price_and_shares_limits():
    """copy-to-my must reject items exceeding max price or calculated max shares (R3)."""
    from app import app
    from app_state import app_state
    from constants import PORTFOLIO_AVG_PRICE_MAX

    orig_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["WTF_CSRF_ENABLED"] = False
    try:
        with patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)):
            client = app.test_client()

            # 1. Target price exceeds PORTFOLIO_AVG_PRICE_MAX
            res = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {"symbol": "HIGHPRICE", "market": "us", "weight_pct": 10.0, "target_price": PORTFOLIO_AVG_PRICE_MAX + 1.0}
                    ]
                },
            )
            assert res.status_code == 400
            data = res.get_json()
            assert "target_price" in data["details"]["reason"]

            with app_state.market.user_stocks_lock:
                assert "HIGHPRICE" not in app_state.market.user_us

            # 2. Calculated shares exceed PORTFOLIO_SHARES_MAX (e.g. extremely tiny target_price like 0.0000001)
            res2 = client.post(
                "/api/ai-portfolio/copy-to-my",
                json={
                    "items": [
                        {"symbol": "MANYSHARES", "market": "us", "weight_pct": 99.0, "target_price": 0.000000001}
                    ]
                },
            )
            assert res2.status_code == 400
            data2 = res2.get_json()
            assert "計算株数が上限" in data2["details"]["reason"] or "上限" in data2["details"]["reason"]

            with app_state.market.user_stocks_lock:
                assert "MANYSHARES" not in app_state.market.user_us
    finally:
        app.config["WTF_CSRF_ENABLED"] = orig_csrf


