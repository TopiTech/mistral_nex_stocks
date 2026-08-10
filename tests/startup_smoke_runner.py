"""Standalone successful-bootstrap smoke runner used by CI.

It is deliberately executed with ``python`` rather than pytest so the normal
test conftest cannot set MNS_SKIP_BOOTSTRAP or replace application executors.
All persisted state is redirected to a temporary directory and external work
is replaced only at the network-scheduling boundary.
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mns-startup-smoke-") as temp_dir:
        temp_path = Path(temp_dir)
        os.environ["MNS_DATA_DIR"] = temp_dir
        os.environ["MNS_APP_DATA_DIR"] = temp_dir
        os.environ.pop("MNS_SKIP_BOOTSTRAP", None)
        os.environ["MNS_MASTER_KEY"] = Fernet.generate_key().decode("ascii")
        os.environ["FLASK_SECRET_KEY"] = secrets.token_hex(32)

        import app as app_module
        import app_bg
        import config_store
        import session_manager
        from app_state import app_state
        from services.realtime_engine import realtime_market_engine
        from utils import storage

        # Never inspect or migrate any workspace-local state during the smoke.
        config_store.LEGACY_CONFIG_FILE = temp_path / "legacy-config.json"
        storage.LEGACY_USER_STOCKS_FILE = str(temp_path / "legacy-user-stocks.json")
        app_state.shutdown_manager.token_file = temp_path / ".mns_shutdown_token"
        app_state.shutdown_manager.used_marker = temp_path / ".mns_shutdown_token.used"

        scheduled_sync = threading.Event()
        scheduled_news = threading.Event()
        thread_started = threading.Event()

        def controlled_background_loop() -> None:
            thread_started.set()
            app_state.execution.shutdown_event.wait(5)

        with (
            patch.object(app_bg, "bg_yahoo_fetch_loop", controlled_background_loop),
            patch.object(app_bg, "bg_leader_election_loop", controlled_background_loop),
            patch.object(app_bg, "bg_interpolate_loop", controlled_background_loop),
            patch.object(session_manager, "bg_session_reap_loop", controlled_background_loop),
            patch.object(realtime_market_engine, "register_symbols"),
            patch.object(realtime_market_engine, "start"),
            patch.object(app_module, "schedule_sync_all_stocks_now", scheduled_sync.set),
            patch.object(app_module, "schedule_news_warmup", scheduled_news.set),
        ):
            app_module.bootstrap(app_module.app)

        assert app_state.bootstrap_ready.is_set(), "bootstrap did not mark the app ready"
        assert app_state.shutdown_manager.token_file.exists(), "shutdown token was not initialized"
        assert thread_started.wait(5), "background thread was not started"
        assert len(app_state.execution.background_threads) >= 5, "background loops were not started"
        assert scheduled_sync.wait(5), "initial sync job was not submitted"
        assert scheduled_news.wait(5), "initial news warmup job was not submitted"

        with app_module.app.test_client() as client:
            response = client.get("/api/health")
        assert response.status_code == 200 and response.is_json, "health endpoint is unavailable"

        app_state.shutdown_executors()
        assert not any(thread.is_alive() for thread in app_state.execution.background_threads)
        # The application logger owns files under the temporary data directory
        # on Windows; close handlers before TemporaryDirectory removes it.
        logging.shutdown()


if __name__ == "__main__":
    main()
