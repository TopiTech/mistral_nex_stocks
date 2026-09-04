"""Regression tests for review findings R2 and R3.

R2 (watchlist cardinality): every watchlist mutator (main add, extension add,
AI copy-to-my) must enforce MAX_USER_WATCHLIST_ITEMS per market inside
user_stocks_lock and before any state change. Default display stocks are not
stored in the user containers and therefore never consume capacity. Overflow
is a fixed 400 (ErrorCode.INVALID_INPUT) and bulk adds must never partially
apply.

R3 (keyboard accessibility): stock cards get an explicit
<button class="compact-expand-btn"> wired to the detail drawer so the main
detail-expansion action works with Enter/Space. The button carries an
accessible name with the symbol and market, and aria-expanded/aria-controls
track the drawer.
"""

import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app_state import app_state
from constants import MAX_USER_WATCHLIST_ITEMS

ROOT = Path(__file__).resolve().parents[1]


def _make_app():
    app = create_app(skip_bootstrap=True)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    return app


def _patch_watchlist_mutations():
    """Patch the side effects of a successful watchlist mutation."""
    return [
        patch("routes.api_stocks.save_user_stocks", return_value=None),
        patch("routes.api_stocks.schedule_sync_all_stocks_now", return_value=None),
        patch("routes.api_stocks._announce_watchlist_state", return_value=None),
        patch("routes.api_stocks._sync_realtime_symbol", return_value=None),
        patch("routes.api_stocks.invalidate_stock_caches", return_value=None),
        patch("routes.api_stocks.ensure_stock_placeholder_in_caches", return_value=None),
    ]


def _fill_market(market, count):
    """Reset all user containers and fill ``market`` with ``count`` entries."""
    with app_state.market.user_stocks_lock:
        app_state.market.user_us = {}
        app_state.market.user_jp = {}
        app_state.market.user_idx = {}
        container = {
            "us": app_state.market.user_us,
            "jp": app_state.market.user_jp,
            "idx": app_state.market.user_idx,
        }[market]
        for i in range(count):
            container[f"SYM{i:03d}"] = f"Stock {i}"


# ---------------------------------------------------------------------------
# R2: watchlist cardinality
# ---------------------------------------------------------------------------


def test_r2_main_add_rejects_at_cap_and_accepts_just_below():
    app = _make_app()
    with (
        app.test_client() as client,
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
    ):
        with ExitStack() as stack:
            for p in _patch_watchlist_mutations():
                stack.enter_context(p)
            _fill_market("us", MAX_USER_WATCHLIST_ITEMS)

            # Exactly at the cap: 400, state untouched.
            res = client.post(
                "/api/stocks/add",
                json={"symbol": "ZZZZ", "market": "us", "name": "Z Corp"},
            )
            assert res.status_code == 400
            data = res.get_json()
            assert data["ok"] is False
            assert f"最大 {MAX_USER_WATCHLIST_ITEMS} 件" in data["details"]["reason"]
            with app_state.market.user_stocks_lock:
                assert "ZZZZ" not in app_state.market.user_us
                assert len(app_state.market.user_us) == MAX_USER_WATCHLIST_ITEMS

            # One slot free: the same add succeeds.
            with app_state.market.user_stocks_lock:
                app_state.market.user_us.pop("SYM000")
            res2 = client.post(
                "/api/stocks/add",
                json={"symbol": "ZZZZ", "market": "us", "name": "Z Corp"},
            )
            assert res2.status_code == 200
            with app_state.market.user_stocks_lock:
                assert "ZZZZ" in app_state.market.user_us
                assert len(app_state.market.user_us) == MAX_USER_WATCHLIST_ITEMS

    with app_state.market.user_stocks_lock:
        app_state.market.user_us = {}


def test_r2_cap_is_per_market_and_defaults_do_not_count():
    app = _make_app()
    with (
        app.test_client() as client,
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
    ):
        with ExitStack() as stack:
            for p in _patch_watchlist_mutations():
                stack.enter_context(p)
            # us is full; adding a jp symbol must still succeed, and a default
            # display stock (NVDA lives in DEFAULT_US, not the user container)
            # must not consume capacity.
            _fill_market("us", MAX_USER_WATCHLIST_ITEMS)

            res = client.post(
                "/api/stocks/add",
                json={"symbol": "6501.T", "market": "jp", "name": "日立製作所"},
            )
            assert res.status_code == 200
            with app_state.market.user_stocks_lock:
                assert "6501.T" in app_state.market.user_jp
                assert len(app_state.market.user_us) == MAX_USER_WATCHLIST_ITEMS

    with app_state.market.user_stocks_lock:
        app_state.market.user_us = {}
        app_state.market.user_jp = {}


def test_r2_extension_add_rejects_at_cap():
    app = _make_app()
    with (
        app.test_client() as client,
        patch(
            "routes.api_stocks.get_or_create_extension_api_token",
            return_value="extension-test-token",
        ),
        patch("utils.networking._is_allowed_shutdown_origin", return_value=True),
    ):
        with ExitStack() as stack:
            for p in _patch_watchlist_mutations():
                stack.enter_context(p)
            _fill_market("us", MAX_USER_WATCHLIST_ITEMS)

            res = client.post(
                "/api/stocks/add_ext",
                json={"symbol": "ZZZZ", "market": "us", "name": "Z Corp"},
                headers={
                    "Authorization": "Bearer extension-test-token",
                    "X-MNS-Extension-Request": "true",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
            assert res.status_code == 400
            data = res.get_json()
            assert data["ok"] is False
            assert f"最大 {MAX_USER_WATCHLIST_ITEMS} 件" in data["details"]["reason"]
            with app_state.market.user_stocks_lock:
                assert "ZZZZ" not in app_state.market.user_us
                assert len(app_state.market.user_us) == MAX_USER_WATCHLIST_ITEMS

    with app_state.market.user_stocks_lock:
        app_state.market.user_us = {}


def test_r2_copy_to_my_rejects_bulk_add_over_cap_without_partial_apply():
    app = _make_app()
    base_us = {f"SYM{i:03d}": f"Stock {i}" for i in range(MAX_USER_WATCHLIST_ITEMS - 5)}
    with (
        app.test_client() as client,
        patch("routes.api_stocks.require_trusted_or_admin", return_value=(True, None)),
    ):
        with ExitStack() as stack:
            for p in _patch_watchlist_mutations():
                stack.enter_context(p)
            with app_state.market.user_stocks_lock:
                app_state.market.user_us = dict(base_us)
                app_state.market.user_jp = {}
                app_state.market.user_idx = {}
            app_state.market.last_usdjpy_rate = 150.0
            app_state.market.last_usdjpy_rate_ts = time.time()

            # 10 new US symbols on a 95/100 market: the whole request must be
            # rejected (400) with nothing applied.
            items = [
                {
                    "symbol": f"NEW{i:02d}",
                    "market": "us",
                    "weight_pct": 10.0,
                    "target_price": 100.0,
                }
                for i in range(10)
            ]
            res = client.post("/api/ai-portfolio/copy-to-my", json={"items": items})
            assert res.status_code == 400
            data = res.get_json()
            assert data["ok"] is False
            assert f"最大 {MAX_USER_WATCHLIST_ITEMS} 件" in data["details"]["reason"]
            with app_state.market.user_stocks_lock:
                assert app_state.market.user_us == base_us
                assert app_state.market.user_jp == {}

            # Same payload split 5 us + 5 jp stays within each market cap.
            items2 = [
                {
                    "symbol": f"NEW{i:02d}",
                    "market": "us" if i < 5 else "jp",
                    "weight_pct": 10.0,
                    "target_price": 100.0,
                }
                for i in range(10)
            ]
            res2 = client.post("/api/ai-portfolio/copy-to-my", json={"items": items2})
            assert res2.status_code == 200
            with app_state.market.user_stocks_lock:
                assert len(app_state.market.user_us) == MAX_USER_WATCHLIST_ITEMS
                assert len(app_state.market.user_jp) == 5

    with app_state.market.user_stocks_lock:
        app_state.market.user_us = {}
        app_state.market.user_jp = {}


# ---------------------------------------------------------------------------
# R3: keyboard accessibility of the stock card detail expansion
# ---------------------------------------------------------------------------


def _ui_source() -> str:
    return (ROOT / "static/js/ui.js").read_text(encoding="utf-8")


def test_r3_compact_card_has_explicit_keyboard_expand_button():
    source = _ui_source()
    assert 'createEl("button", "compact-expand-btn", "詳細")' in source
    assert 'expandBtn.type = "button"' in source
    assert 'expandBtn.setAttribute("aria-expanded", "false")' in source
    assert 'expandBtn.setAttribute("aria-controls", "stock-detail-drawer-overlay")' in source
    # Accessible name must include the symbol and the market.
    assert "`${stock.symbol}（${market}）の詳細を開く`" in source
    # The button must be wired to the same drawer action as the card click.
    assert "openStockDetailDrawer(getLatestStockForDrawer(stock, wrapper), wrapper)" in source


def test_r3_card_click_ignores_expand_button_to_avoid_double_fire():
    source = _ui_source()
    assert 'e.target.closest(".compact-expand-btn")' in source
    assert "e.stopPropagation()" in source


def test_r3_drawer_open_close_tracks_aria_expanded_on_expand_button():
    source = _ui_source()
    # Opening the drawer marks the card's expand button as expanded...
    assert 'drawerExpandBtn.setAttribute("aria-expanded", "true")' in source
    # ...and closing it resets the state and the accessible label.
    assert 'drawerExpandBtn.setAttribute("aria-expanded", "false")' in source
    assert "の詳細を閉じる" in source
    assert "の詳細を開く" in source
    # The inline detail-panel toggler must not fight the drawer-owned button.
    assert (
        'wrapper.querySelector(".compact-expand-btn")'
        not in source.split("function toggleDetail")[1].split("function closeDetailPanel")[0]
    )


def test_r3_expand_button_survives_card_updates():
    # SSE/realtime updates reuse the existing wrapper (updateExistingCard ->
    # updateStockUI), so the focusable expand button is never rebuilt.
    source = _ui_source()
    assert "const updateExistingCard = (wrapper, stock) => updateStockUI(wrapper, stock)" in source
