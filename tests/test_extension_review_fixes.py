"""Static regression guards for extension review findings R1/R4-R7."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app_state import app_state

ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


class _ElementAttributeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.attributes_by_id: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.attributes_by_id[element_id] = attributes


def test_r1_detector_is_explicitly_injected_without_all_site_content_script():
    manifest = json.loads(_read("chrome_extension/manifest.json"))
    assert "activeTab" in manifest["permissions"]
    assert "scripting" in manifest["permissions"]
    assert "content_scripts" not in manifest
    assert "http://*/*" not in manifest.get("host_permissions", [])
    assert "https://*/*" not in manifest.get("host_permissions", [])
    popup = _read("chrome_extension/popup.js")
    assert "chrome.scripting.executeScript" in popup
    assert 'files: ["content.js"]' in popup


def test_security_md_describes_on_demand_injection_without_all_urls():
    """SECURITY.md must not claim `<all_urls>` content-script access.

    The actual model (validated by test_r1_detector_is_explicitly_injected_*)
    is on-demand injection via chrome.scripting.executeScript under the
    activeTab permission, with loopback-only host_permissions. The security
    doc must describe that real posture rather than a broader `<all_urls>`
    surface that does not exist.
    """
    security = _read("SECURITY.md")
    content_script_section = security[security.index("Chrome extension") :]
    content_script_section = content_script_section[: content_script_section.index("\n- **Native")]
    # The doc must not present `<all_urls>` as the actual access model.
    assert "content script uses `<all_urls>` host access" not in content_script_section
    assert "does **not** use `<all_urls>`" in content_script_section
    assert "chrome.scripting.executeScript" in content_script_section
    assert "activeTab" in content_script_section
    assert "loopback" in content_script_section


def test_r4_native_host_uses_installer_generated_launcher_path():
    template = json.loads(_read("native_host/com.mistral_nex_stocks.host.json.template"))
    assert template["path"] == "__LAUNCHER_PATH__"
    assert "__PYTHON_EXE__" in _read("native_host/host_launcher.cmd.template")
    installer = _read("native_host/install_host_windows.ps1")
    assert "com.mistral_nex_stocks.host.json.template" in installer
    assert "Replace('__LAUNCHER_PATH__'" in installer


def test_r5_failed_refresh_marks_existing_stock_data_stale():
    popup = _read("chrome_extension/popup.js")
    assert 'stockContainer.classList.add("stale")' in popup
    assert "表示中のデータは古い可能性があります" in popup
    assert ".stock-list-container.stale" in _read("chrome_extension/popup.css")


def test_r6_stock_rows_have_keyboard_activation_and_focus_style():
    popup = _read("chrome_extension/popup.js")
    css = _read("chrome_extension/popup.css")
    assert 'setAttribute("role", "button")' in popup
    assert 'setAttribute("tabindex", "0")' in popup
    assert 'event.key === "Enter"' in popup
    assert 'event.key === " "' in popup
    assert ".stock-item:focus-visible" in css


def test_stock_details_deep_link_preserves_symbol():
    popup = _read("chrome_extension/popup.js")
    assert "encodeURIComponent(symbol)" in popup
    assert "/main?q=" in popup


def test_index_deep_links_use_canonical_symbols_and_idx_market():
    popup = _read("chrome_extension/popup.js")
    for symbol in ("^N225", "^DJI", "USDJPY=X", "^GSPC", "^IXIC"):
        assert f'symbol: "{symbol}"' in popup
    assert 'renderStockItem(symbol, name, item.price, pct, "idx")' in popup


def test_extension_renders_custom_index_collection():
    popup = _read("chrome_extension/popup.js")
    assert "allStocksData.stocks?.idx" in popup
    assert '...idxStocks.map((stock) => ({ ...stock, market: "idx" }))' in popup


def test_extension_service_worker_starts_without_session_storage_api():
    """The worker must still initialize when storage.session is unavailable."""
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the extension runtime regression test")

    script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("chrome_extension/background.js", "utf8");
const prefix = source.split("let mnsExtensionTokenInflight = null;")[0];
const context = {
  chrome: {
    storage: {
      local: {
        get: (_keys, callback) => callback({}),
        remove: () => undefined,
      },
    },
  },
  console,
};
vm.runInNewContext(`${prefix}\nsetMnsExtensionToken("sentinel");`, context);
console.log("initialized");
'''
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("initialized")


def test_invalid_context_menu_selection_is_not_logged_and_keeps_rejection_ui():
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the extension runtime regression test")

    script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("chrome_extension/background.js", "utf8");
const start = source.indexOf("chrome.contextMenus.onClicked.addListener");
const end = source.indexOf("// Badge Updates", start);
if (start < 0 || end < 0) throw new Error("context menu handler source not found");
const warnings = [];
const badgeCalls = [];
const context = {
  chrome: {
    contextMenus: {
      onClicked: { addListener(listener) { context.onClicked = listener; } },
    },
  },
  console: { warn(...args) { warnings.push(args); } },
  setBadgeMessage(...args) { badgeCalls.push(args); },
};
vm.runInNewContext(source.slice(start, end), context);
const selection = ["external", "page", "selection"].join(" ");
(async () => {
  await context.onClicked({ menuItemId: "add-us-stock", selectionText: selection });
  if (warnings.length !== 1) throw new Error("expected one rejection warning");
  if (warnings.some((args) => args.some((arg) => String(arg).includes(selection)))) {
    throw new Error("rejection warning contains selected text");
  }
  if (badgeCalls.length !== 1 || badgeCalls[0][0] !== "NG" || badgeCalls[0][1] !== "#ff7d7d") {
    throw new Error("invalid selection did not retain rejection badge");
  }
  process.stdout.write("ok");
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
'''
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


def test_restarting_stock_poll_while_a_fetch_is_pending_keeps_one_poll_chain():
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the extension runtime regression test")

    script = r'''
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync("chrome_extension/popup.js", "utf8");
const variablesStart = source.indexOf("let stockPollInterval");
const variablesEnd = source.indexOf("// Tab Switching", variablesStart);
const start = source.indexOf("function startStockPolling");
const end = source.indexOf("function setHealth", start);
if (variablesStart < 0 || variablesEnd < 0 || start < 0 || end < 0) {
  throw new Error("polling lifecycle source not found");
}
const lifecycle = source.slice(variablesStart, variablesEnd) + source.slice(start, end);
const timers = [];
const pending = [];
let fetchCalls = 0;
const context = {
  setTimeout(fn, delay) {
    const timer = { fn, delay, cleared: false };
    timers.push(timer);
    return timer;
  },
  clearTimeout(timer) { if (timer) timer.cleared = true; },
  fetchAndRenderStocks(base) {
    fetchCalls += 1;
    return new Promise((resolve) => pending.push({ base, resolve }));
  },
};
vm.createContext(context);
vm.runInContext(`${lifecycle}\nthis.startStockPolling = startStockPolling; this.stopStockPolling = stopStockPolling;`, context);
(async () => {
  context.startStockPolling("http://loopback");
  context.startStockPolling("http://loopback");
  if (fetchCalls !== 2 || pending.length !== 2) throw new Error("expected two pending initial fetches");
  pending.splice(0, 2).forEach(({ resolve }) => resolve());
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  const live = timers.filter((timer) => !timer.cleared);
  if (live.length !== 1) throw new Error(`expected one live timer, got ${live.length}`);
  live[0].fn();
  if (fetchCalls !== 3) throw new Error(`expected one next poll, got ${fetchCalls} fetches`);
  context.stopStockPolling();
  pending.shift().resolve();
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
  if (timers.some((timer) => !timer.cleared)) throw new Error("stop left a live polling timer");
  process.stdout.write("ok");
})().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
'''
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ok"


def test_popup_tabs_support_aria_keyboard_navigation():
    popup = _read("chrome_extension/popup.js")
    for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
        assert f'event.key === "{key}"' in popup
    assert "b.tabIndex = -1" in popup


def test_popup_main_launcher_targets_dashboard_route():
    popup = _read("chrome_extension/popup.js")

    assert '$("openMainBtn")?.addEventListener("click", () => openAppPage("/main"));' in popup


def test_r7_tabs_and_panels_are_connected_with_hidden_semantics():
    html = _read("chrome_extension/popup.html")
    popup = _read("chrome_extension/popup.js")
    parser = _ElementAttributeParser()
    parser.feed(html)
    assert 'role="tablist"' in html
    assert 'aria-controls="tab-content-stocks"' in html
    assert 'aria-labelledby="tab-stocks"' in html
    for panel_id in ("tab-content-detector", "tab-content-system"):
        attributes = parser.attributes_by_id[panel_id]
        assert attributes["aria-hidden"] == "true"
        assert "hidden" in attributes
    assert 'c.setAttribute("aria-hidden", "true")' in popup
    assert 'contentEl.setAttribute("aria-hidden", "false")' in popup


def test_add_ext_does_not_duplicate_legacy_numeric_jp_symbol():
    """R1: normalized extension input must respect legacy JP ticker aliases."""
    app = create_app(skip_bootstrap=True)
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    app_state.market.user_jp = {"1234": "Legacy Tokyo Stock"}

    try:
        with app.test_client() as client, patch(
            "routes.api_stocks.get_or_create_extension_api_token",
            return_value="extension-test-token",
        ), patch("routes.api_stocks.save_user_stocks"), patch(
            "routes.api_stocks.schedule_sync_all_stocks_now"
        ), patch("utils.networking._is_allowed_shutdown_origin", return_value=True):
            response = client.post(
                "/api/stocks/add_ext",
                json={"symbol": "1234", "market": "jp", "name": "Canonical Tokyo Stock"},
                headers={
                    "Authorization": "Bearer extension-test-token",
                    "X-MNS-Extension-Request": "true",
                },
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )

        assert response.status_code == 200
        assert response.get_json()["message"] == "1234.T already exists in jp"
        assert app_state.market.user_jp == {"1234": "Legacy Tokyo Stock"}
    finally:
        app_state.market.user_jp = {}
