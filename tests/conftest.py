import pytest

@pytest.fixture(scope="session", autouse=True)
def shutdown_app_state():
    yield
    try:
        from app_state import app_state
        app_state.shutdown_executors()
    except Exception:
        pass

@pytest.fixture(autouse=True)
def reset_app_state():
    from app_state import app_state

    def do_reset():
        # Reset Mistral rate limit/streak
        if hasattr(app_state, "ai"):
            app_state.ai.mistral_429_streak = 0
            app_state.ai.mistral_next_allowed_ts = 0.0
            app_state.ai.mistral_last_call_ts = 0.0
            if hasattr(app_state.ai, "mistral_response_cache"):
                app_state.ai.mistral_response_cache.clear()

        # Reset yfinance rate limit and circuits
        if hasattr(app_state, "market"):
            app_state.market.is_yfinance_rate_limited = False
            app_state.market.yfinance_rate_limit_until = 0.0
            app_state.market.yfinance_last_request_ts = 0.0
            app_state.market.yfinance_429_streak = 0
            app_state.market.circuit_states = {
                "mistral": {"status": "CLOSED", "timeout_streak": 0, "open_until": 0.0},
                "langsearch": {"status": "CLOSED", "timeout_streak": 0, "open_until": 0.0},
            }
            if hasattr(app_state.market, "history_circuit_state"):
                app_state.market.history_circuit_state.clear()

        # Reset cache stats
        if hasattr(app_state, "cache"):
            app_state.cache.reset_stats()

    do_reset()
    try:
        yield
    finally:
        do_reset()

