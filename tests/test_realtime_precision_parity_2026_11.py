"""Regression: market-aware realtime change precision (backend/frontend parity).

Backend `RealtimeMarketEngine` uses 2 decimals for JP and 4 for US.
Frontend `realtime_client.js` previously rounded every change to 2 decimals,
destroying US sub-cent precision, and `ui.js` display did the same.
`engine.py` also used `str.replace(".T", "")` which removes every occurrence.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).parent.parent


def _run_node(script: str) -> str:
    res = subprocess.run(
        ["node", "-e", script],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"Node failed:\nstdout={res.stdout}\nstderr={res.stderr}"
    return res.stdout.strip()


def test_engine_suffix_handling_is_case_insensitive() -> None:
    from services.realtime.engine import RealtimeMarketEngine

    engine = RealtimeMarketEngine.__new__(RealtimeMarketEngine)
    import threading

    engine.market_store = {}
    engine.previous_store = {}
    engine._dirty_symbols = set()
    engine._client_pending = {}
    engine._client_events = {}
    engine.store_lock = threading.RLock()
    engine._registration_tokens = {}
    engine._engine_epoch = 0

    with patch(
        "services.realtime.engine._get_yfinance_previous_close",
        return_value=None,
    ), patch(
        "services.realtime.engine._get_yfinance_volume",
        return_value=None,
    ):
        # Lowercase suffix must still be treated as JP (2 decimals).
        engine._handle_producer_update({"symbol": "7203.t", "change": 12.3456})
        assert engine.market_store["7203.t"]["change"] == 12.35
        # Bare JP alphanumeric code without suffix stays JP.
        engine._handle_producer_update({"symbol": "130A", "change": 1.23456})
        assert engine.market_store["130A"]["change"] == 1.23
        # US symbol keeps 4 decimals.
        engine._handle_producer_update({"symbol": "AAPL", "change": 6.66744})
        assert engine.market_store["AAPL"]["change"] == 6.6674


def test_realtime_client_preserves_us_precision() -> None:
    script = r"""
    const fs = require('fs');
    const path = require('path');
    const vm = require('vm');
    const src = fs.readFileSync(path.join(process.cwd(), 'static', 'js', 'realtime_client.js'), 'utf-8');
    const context = {
      window: { requestAnimationFrame: (fn) => fn() },
      document: { addEventListener(){}, readyState: 'complete' },
      CSS: undefined,
      requestAnimationFrame: (fn) => fn(),
      clearTimeout: () => {}, setTimeout: (fn) => 0,
    };
    vm.createContext(context);
    vm.runInContext(src, context);
    const client = new context.window.realtimeClient.constructor();
    // Stub DOM lookup: no wrappers, focus on data mutation.
    client._getElements = () => ({ bareSymbol: 'AAPL', cleanCode: 'AAPL', wrappers: [], ptsElements: [] });
    const deltas = { AAPL: { price: 240.1234, change: 6.6674, change_percent: 2.854 } };
    // requestAnimationFrame runs synchronously in this harness.
    client.handleDeltas(deltas);
    const ch = deltas.AAPL.change;
    if (ch !== 6.6674) { console.error('US change truncated:', ch); process.exit(1); }
    // JP symbol must round to 2 decimals.
    client._getElements = () => ({ bareSymbol: '7203.T', cleanCode: '7203', wrappers: [], ptsElements: [] });
    const jp = { '7203.T': { price: 1000, change: 12.3456, change_percent: 1.234 } };
    client.handleDeltas(jp);
    if (jp['7203.T'].change !== 12.35) { console.error('JP change wrong:', jp['7203.T'].change); process.exit(2); }
    console.log('OK');
    """
    assert _run_node(script) == "OK"


def test_format_stock_change_metrics_market_aware() -> None:
    script = r"""
    const fs = require('fs');
    const path = require('path');
    const vm = require('vm');
    const src = fs.readFileSync(path.join(process.cwd(), 'static', 'js', 'ui.js'), 'utf-8');
    const match = src.match(/function formatStockChangeMetrics\([\s\S]*?\n\}/);
    if (!match) { console.error('not found'); process.exit(1); }
    const ctx = {};
    vm.createContext(ctx);
    vm.runInContext(match[0], ctx);
    const us = ctx.formatStockChangeMetrics({ symbol: 'AAPL', change: 6.6674, change_percent: 2.854 });
    if (!us.text.includes('6.6674')) { console.error('US display truncated:', us); process.exit(2); }
    const jp = ctx.formatStockChangeMetrics({ symbol: '7203.T', market: 'jp', change: 12.3456, change_percent: 1.234 });
    if (!jp.text.includes('12.35')) { console.error('JP display wrong:', jp); process.exit(3); }
    console.log('OK');
    """
    assert _run_node(script) == "OK"
