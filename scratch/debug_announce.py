from unittest.mock import patch

import app_bg
from app_state import app_state

with (
    patch.object(app_state.sse_announcer, "announce") as mock_announce,
    patch("app_bg.is_market_open", side_effect=lambda m: m == "us"),
):
    print("sse_announcer is mock_announce?", app_state.sse_announcer.announce == mock_announce)
    app_bg._invalidate_sse_payload_cache()
    app_bg._sse_full_snapshot_counter = 5
    app_bg._original_announce_current_market_state()
    print("mock_announce.called:", mock_announce.called)
    print("mock_announce.call_args:", mock_announce.call_args)
