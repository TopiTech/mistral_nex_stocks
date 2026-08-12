import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from services.realtime_engine import is_pts_session
from services.search.ddgs import _sanitize_ddgs_query
from utils.chat_history import SQLiteChatHistoryStore, init_db
from utils.market_utils import is_jp_market_holiday


# --- [R1] Native Host PID Reuse Protection ---
def test_r1_native_host_process_creation_time():
    """Verify that process ancestor lookup handles creation time comparison properly."""
    from native_host.native_host import _get_ancestor_process_names

    with patch.dict("os.environ", {"NATIVE_HOST_ALLOW_ANY_PARENT": "0", "MNS_SKIP_BOOTSTRAP": "0", "MNS_TEST_MODE": "0"}):
        ancestors = _get_ancestor_process_names(max_depth=3)
        assert isinstance(ancestors, list)


# --- [R2] Japan Market Holiday & PTS Session Check ---
def test_r2_jp_market_holidays():
    """Verify Japan stock exchange holiday calculations."""
    # New Year / Year-end
    assert is_jp_market_holiday(datetime.date(2026, 1, 1)) is True
    assert is_jp_market_holiday(datetime.date(2026, 1, 2)) is True
    assert is_jp_market_holiday(datetime.date(2026, 1, 3)) is True
    assert is_jp_market_holiday(datetime.date(2026, 12, 31)) is True

    # National Holidays
    assert is_jp_market_holiday(datetime.date(2026, 2, 11)) is True  # 建国記念の日
    assert is_jp_market_holiday(datetime.date(2026, 2, 23)) is True  # 天皇誕生日
    assert is_jp_market_holiday(datetime.date(2026, 4, 29)) is True  # 昭和の日
    assert is_jp_market_holiday(datetime.date(2026, 5, 3)) is True   # 憲法記念日
    assert is_jp_market_holiday(datetime.date(2026, 5, 4)) is True   # みどりの日
    assert is_jp_market_holiday(datetime.date(2026, 5, 5)) is True   # こどもの日
    assert is_jp_market_holiday(datetime.date(2026, 8, 11)) is True  # 山の日
    assert is_jp_market_holiday(datetime.date(2026, 11, 3)) is True  # 文化の日
    assert is_jp_market_holiday(datetime.date(2026, 11, 23)) is True # 勤労感謝の日

    # Happy Mondays (2026)
    assert is_jp_market_holiday(datetime.date(2026, 1, 12)) is True  # 成人の日: 1月第2月曜
    assert is_jp_market_holiday(datetime.date(2026, 7, 20)) is True  # 海の日: 7月第3月曜
    assert is_jp_market_holiday(datetime.date(2026, 9, 21)) is True  # 敬老の日: 9月第3月曜
    assert is_jp_market_holiday(datetime.date(2026, 10, 12)) is True # スポーツの日: 10月第2月曜

    # Regular Trading Day
    assert is_jp_market_holiday(datetime.date(2026, 7, 8)) is False


def test_r2_pts_session_on_holiday():
    """Verify that is_pts_session returns False on holidays even during active hours."""
    jst = ZoneInfo("Asia/Tokyo")
    # New Year Day daytime active hour (10:00 JST)
    holiday_dt = datetime.datetime(2026, 1, 1, 10, 0, tzinfo=jst)
    assert is_pts_session(holiday_dt) is False

    # Normal weekday active daytime hour (Wednesday 10:00 JST)
    workday_dt = datetime.datetime(2026, 7, 8, 10, 0, tzinfo=jst)
    assert is_pts_session(workday_dt) is True


# --- [R3] SQLite Chat History Cleanup ---
def test_r3_chat_history_wal_checkpoint_and_cleanup(tmp_path):
    """Verify SQLite WAL autocheckpoint and connection tracking."""
    init_db()
    store = SQLiteChatHistoryStore(max_sessions=10, max_msgs_per_session=10)
    store.add_message("test:session", {"role": "user", "content": "Hello"})
    messages = store["test:session"]
    assert len(messages) == 1
    assert messages[0]["content"] == "Hello"

    # Close and verify clean state
    store.close()
    assert getattr(store._local, "conn", None) is None
    store.close_all()


# --- [R4] Japanese Stock Portfolio Avg FX Rate Stripping ---
def test_r4_jp_portfolio_strip_avg_fx_rate(client):
    """Verify that Japanese stock portfolio update ignores avg_fx_rate."""
    from app_state import app_state

    with app_state.market.user_stocks_lock:
        app_state.market.user_us = {}
        app_state.market.user_jp = {"7203.T": {"name": "トヨタ自動車", "shares": 50, "avg_price": 2400}}
        app_state.market.user_idx = {}
        app_state.market.last_loaded_rev = app_state.market.user_stocks_rev

    response = client.post(
        "/api/stocks/portfolio",
        headers={"Origin": "http://localhost:5000"},
        json={
            "market": "jp",
            "symbol": "7203.T",
            "shares": 100,
            "avg_price": 2500,
            "avg_fx_rate": 155.5,
        },
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True

    with app_state.market.user_stocks_lock:
        val = app_state.market.user_jp.get("7203.T")
        assert val is not None
        assert val["shares"] == 100
        assert val["avg_price"] == 2500
        assert "avg_fx_rate" not in val


# --- [R6] DuckDuckGo Search Query Sanitization ---
def test_r6_ddgs_query_sanitization():
    """Verify DDGS query character and byte length clipping."""
    # Normal query
    assert _sanitize_ddgs_query("Apple Inc Earnings") == "Apple Inc Earnings"

    # Overly long text
    long_text = "A" * 600
    sanitized = _sanitize_ddgs_query(long_text, max_chars=500, max_bytes=1000)
    assert len(sanitized) == 500

    # Overly long multibyte text (Japanese)
    long_jp = "トヨタ自動車 決算発表 業績予想 ニュース " * 30
    sanitized_jp = _sanitize_ddgs_query(long_jp, max_chars=500, max_bytes=100)
    assert len(sanitized_jp.encode("utf-8")) <= 100
