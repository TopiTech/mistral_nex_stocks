"""Test package for unittest discovery.

Common test utilities shared across test modules.
"""

import json
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


def create_temp_config(
    overrides: Optional[Dict[str, Any]] = None,
    api_credentials: Optional[Dict[str, Any]] = None,
) -> Path:
    """Create a temporary config file for testing.

    Args:
        overrides: Dict of config values to override defaults.
        api_credentials: Dict of API credentials to inject.

    Returns:
        Path to the created config file.
    """
    config: Dict[str, Any] = {
        "mistral_model": "mistral-small-latest",
        "model_badge": "mistral-small",
        "api_credentials": api_credentials or {},
    }
    if overrides:
        config.update(overrides)

    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(config, tmp, ensure_ascii=False, indent=2)
    tmp.close()
    return Path(tmp.name)


def reset_app_state_internals():
    """Reset the global app_state to a clean slate for testing."""
    from app_state import app_state

    if hasattr(app_state, "ai"):
        app_state.ai.mistral_429_streak = 0
        app_state.ai.mistral_next_allowed_ts = 0.0
        app_state.ai.mistral_last_call_ts = 0.0
        if hasattr(app_state.ai, "mistral_response_cache"):
            app_state.ai.mistral_response_cache.clear()

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

    if hasattr(app_state, "cache"):
        app_state.cache.reset_stats()
