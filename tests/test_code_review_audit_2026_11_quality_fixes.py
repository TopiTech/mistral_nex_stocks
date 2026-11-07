"""Regression tests for code review audit quality fixes (November 2026).

Covers:
1. ISSUE-01: chrome_extension/popup.css .mt-8 selector definition.
2. ISSUE-02: chrome_extension/popup.js startBtn/stopBtn busyText, healthMeta progress, and finally refresh recovery.
3. ISSUE-03: chrome_extension/popup.js addDetectedStock failure/exception handling with handleUIError, title, and 2.5s timer restoration.
4. ISSUE-04: templates/setup.html & static/js/setup.js Mistral setup connection test UI and verifyMistralSetup handler.
5. ISSUE-05: static/js/ui.js technical indicator buttons in buildDetailPanel have type="button" and aria-pressed attributes.
6. ISSUE-06: static/js/ui.js openFullscreenChart async callbacks guard against hidden modal or mismatched stockKey.
7. ISSUE-07: static/js/screener.js screener addBtn inFlight guard and 2.5s auto-revert timer.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _run_node(script: str) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for executing frontend JavaScript test scripts")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"Node script failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    return result.stdout


# ===========================================================================
# ISSUE-01: chrome_extension/popup.css .mt-8 selector
# ===========================================================================


def test_issue01_popup_css_mt8_selector():
    """Verify that .mt-8 selector is defined in popup.css with margin-top: 8px."""
    css_content = _read("chrome_extension/popup.css")
    html_content = _read("chrome_extension/popup.html")

    # .mt-8 selector must be present with margin-top: 8px
    pattern = re.compile(r"\.mt-8\s*\{\s*margin-top:\s*8px;?\s*\}")
    assert pattern.search(css_content), "popup.css must contain .mt-8 { margin-top: 8px; }"

    # popup.html uses class="mt-8"
    assert 'class="mt-8"' in html_content or "mt-8" in html_content


# ===========================================================================
# ISSUE-02: chrome_extension/popup.js startBtn / stopBtn withBusy & progress
# ===========================================================================


def test_issue02_popup_js_with_busy_and_button_handlers():
    """Verify withBusy supports busyText, progress in healthMeta, and finally refresh recovery."""
    js_content = _read("chrome_extension/popup.js")

    # withBusy definition check
    assert "async function withBusy(btn, fn, busyText = null)" in js_content
    assert "if (busyText)" in js_content
    assert "btn.textContent = busyText;" in js_content
    assert "await refresh();" in js_content

    # startBtn & stopBtn handlers check
    assert '"⏳ 起動中..."' in js_content
    assert "バックエンド起動中... (最大30秒)" in js_content
    assert '"⏳ 停止中..."' in js_content
    assert "バックエンド停止中..." in js_content

    # Node.js simulation test
    node_test = r"""
const assert = require("assert");

let refreshCallCount = 0;
async function refresh() {
  refreshCallCount++;
}

async function withBusy(btn, fn, busyText = null) {
  const prev = btn.textContent;
  btn.disabled = true;
  if (busyText) {
    btn.textContent = busyText;
  }
  try {
    return await fn();
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
    try {
      await refresh();
    } catch (_) {}
  }
}

(async () => {
  const btn = { textContent: "起動", disabled: false };
  let busyTextObserved = "";
  await withBusy(btn, async () => {
    busyTextObserved = btn.textContent;
    assert.strictEqual(btn.disabled, true);
  }, "⏳ 起動中...");

  assert.strictEqual(busyTextObserved, "⏳ 起動中...");
  assert.strictEqual(btn.disabled, false);
  assert.strictEqual(btn.textContent, "起動");
  assert.strictEqual(refreshCallCount, 1);

  // Test error propagation with cleanup
  let caught = false;
  try {
    await withBusy(btn, async () => {
      throw new Error("fail");
    }, "⏳ 起動中...");
  } catch (e) {
    caught = true;
  }
  assert.strictEqual(caught, true);
  assert.strictEqual(btn.disabled, false);
  assert.strictEqual(btn.textContent, "起動");
  assert.strictEqual(refreshCallCount, 2);
  console.log("ISSUE-02 PASS");
})();
"""
    output = _run_node(node_test)
    assert "ISSUE-02 PASS" in output


# ===========================================================================
# ISSUE-03: chrome_extension/popup.js addDetectedStock failure/exception
# ===========================================================================


def test_issue03_popup_js_add_detected_stock_error_handling():
    """Verify addDetectedStock error handling notifies handleUIError and resets after 2.5s."""
    js_content = _read("chrome_extension/popup.js")

    assert 'send("addDetectedStock"' in js_content
    assert "handleUIError(errorMsg);" in js_content
    assert "addBtn.title = errorMsg;" in js_content
    assert 'addBtn.textContent = "失敗";' in js_content
    assert 'addBtn.textContent = "エラー";' in js_content
    assert 'addBtn.textContent = "➕ 追加";' in js_content
    assert "2500" in js_content

    # Node.js simulation test
    node_test = r"""
const assert = require("assert");

let lastUIError = null;
function handleUIError(err) {
  lastUIError = err;
}

async function simulateAdd(ok, errorMsg, isException = false) {
  const addBtn = {
    disabled: false,
    textContent: "➕ 追加",
    title: "",
    className: "mini-add-btn"
  };

  addBtn.disabled = true;
  addBtn.textContent = "追加中...";

  try {
    if (isException) throw new Error("ネットワーク断");
    const response = { ok, error: errorMsg };
    if (response?.ok) {
      addBtn.textContent = "✓ 追加済";
      addBtn.className = "mini-add-btn success";
      addBtn.title = "";
    } else {
      const err = response?.error || "銘柄の追加に失敗しました";
      handleUIError(err);
      addBtn.title = err;
      addBtn.textContent = "失敗";
      addBtn.disabled = false;
      setTimeout(() => {
        if (addBtn.textContent !== "✓ 追加済") {
          addBtn.textContent = "➕ 追加";
        }
      }, 50);
    }
  } catch (err) {
    const errText = err?.message || String(err || "エラー");
    handleUIError(err);
    addBtn.title = errText;
    addBtn.textContent = "エラー";
    addBtn.disabled = false;
    setTimeout(() => {
      if (addBtn.textContent !== "✓ 追加済") {
        addBtn.textContent = "➕ 追加";
      }
    }, 50);
  }
  return addBtn;
}

(async () => {
  // Test failure branch
  const btn1 = await simulateAdd(false, "追加上限超過");
  assert.strictEqual(btn1.textContent, "失敗");
  assert.strictEqual(btn1.title, "追加上限超過");
  assert.strictEqual(lastUIError, "追加上限超過");
  await new Promise(r => setTimeout(r, 80));
  assert.strictEqual(btn1.textContent, "➕ 追加");

  // Test exception branch
  const btn2 = await simulateAdd(false, "", true);
  assert.strictEqual(btn2.textContent, "エラー");
  assert.strictEqual(btn2.title, "ネットワーク断");
  assert.strictEqual(lastUIError.message, "ネットワーク断");
  await new Promise(r => setTimeout(r, 80));
  assert.strictEqual(btn2.textContent, "➕ 追加");

  console.log("ISSUE-03 PASS");
})();
"""
    output = _run_node(node_test)
    assert "ISSUE-03 PASS" in output


# ===========================================================================
# ISSUE-04: templates/setup.html & static/js/setup.js
# ===========================================================================


def test_issue04_setup_html_and_js_mistral_verify_ui():
    """Verify setup.html contains Mistral verify UI and setup.js binds verifyMistralSetup."""
    html_content = _read("templates/setup.html")
    js_content = _read("static/js/setup.js")

    # HTML elements check
    assert 'id="verifyMistralSetupBtn"' in html_content
    assert 'id="mistralSetupVerifyStatus"' in html_content
    assert 'role="status"' in html_content
    assert 'aria-live="polite"' in html_content

    # JS function and binding check
    assert "async function verifyMistralSetup()" in js_content
    assert 'getEl("verifyMistralSetupBtn")?.addEventListener("click", verifyMistralSetup);' in js_content
    assert 'target_provider: "mistral"' in js_content
    assert "mistral_api_mode: apiMode" in js_content
    assert "mistral_agent_id: agentId" in js_content
    assert "接続成功 (${latency}ms)！利用可能モデル: ${modelCount}件" in js_content


def test_issue04_api_credentials_verify_backend_endpoint():
    """Verify /api/credentials/verify handles target_provider=mistral correctly."""
    from app import create_app

    app = create_app()
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False

    client = app.test_client()
    headers = {"Origin": "http://localhost:5000"}

    # 1. Missing API key returns 400 with INVALID_API_KEY
    resp = client.post(
        "/api/credentials/verify",
        json={"target_provider": "mistral", "api_key": ""},
        headers=headers,
    )
    assert resp.status_code == 400
    data = resp.get_json()
    assert data["ok"] is False or data.get("valid") is False

    # 2. Preview key returns 200 with preview=True
    from credential_manager import PREVIEW_API_KEY

    resp = client.post(
        "/api/credentials/verify",
        json={"target_provider": "mistral", "api_key": PREVIEW_API_KEY},
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data.get("preview") is True
    assert data.get("valid") is True

    # 3. Agents SDK mode without agentId is valid and verifies API key
    mock_client = MagicMock()
    mock_client.models.list.return_value = MagicMock(data=[MagicMock(id="mistral-small-latest")])
    with (
        patch("credential_manager.get_agent_id", return_value=""),
        patch("app_state.app_state.ai.get_or_create_mistral_client", return_value=mock_client),
    ):
        resp = client.post(
            "/api/credentials/verify",
            json={
                "target_provider": "mistral",
                "api_key": "mk-real-key-test-value",
                "mistral_api_mode": "agents",
                "mistral_agent_id": "",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("valid") is True
        assert data.get("mode") == "agents"


# ===========================================================================
# ISSUE-05: static/js/ui.js technical indicator buttons type & aria-pressed
# ===========================================================================


def test_issue05_ui_js_technical_indicator_buttons():
    """Verify buildDetailPanel technical indicator buttons set type='button' and aria-pressed."""
    js_content = _read("static/js/ui.js")

    # Locate buildDetailPanel indicator loop
    assert 'btn.type = "button";' in js_content
    assert 'btn.setAttribute("aria-pressed", String(isOn));' in js_content

    # Ensure this occurs in the indicators.forEach loop in buildDetailPanel
    indicator_pattern = re.compile(
        r"indicators\.forEach\(\(ind\)\s*=>\s*\{[\s\S]*?btn\.type\s*=\s*\"button\";[\s\S]*?btn\.setAttribute\(\"aria-pressed\",\s*String\(isOn\)\);[\s\S]*?indGroup\.appendChild\(btn\);"
    )
    assert indicator_pattern.search(js_content), "buildDetailPanel must set type and aria-pressed on indicator buttons"


# ===========================================================================
# ISSUE-06: static/js/ui.js openFullscreenChart cancel guards
# ===========================================================================


def test_issue06_ui_js_fullscreen_modal_cancel_guard():
    """Verify openFullscreenChart setTimeout and refreshStockChart callbacks have modal cancel guard."""
    js_content = _read("static/js/ui.js")

    guard_pattern = re.compile(
        r"modal\.classList\.contains\(\"hidden\"\)\s*\|\|\s*modal\.dataset\.stockKey\s*!==\s*stockKey"
    )
    matches = len(guard_pattern.findall(js_content))
    # Must be present in both the setTimeout and refreshStockChart.then callback in openFullscreenChart
    assert matches >= 2, f"Expected at least 2 occurrences of modal cancel guard, found {matches}"


# ===========================================================================
# ISSUE-07: static/js/screener.js screener addBtn inFlight guard & auto-revert
# ===========================================================================


def test_issue07_screener_js_in_flight_and_auto_revert():
    """Verify screener.js addBtn has inFlight guard, finally cleanup, and 2.5s auto-revert timer."""
    js_content = _read("static/js/screener.js")

    assert 'if (addBtn.dataset.inFlight === "true") return;' in js_content
    assert 'addBtn.dataset.inFlight = "true";' in js_content
    assert "delete addBtn.dataset.inFlight;" in js_content
    assert "2500" in js_content

    # Node.js simulation test of inFlight debounce and timer revert
    node_test = r"""
const assert = require("assert");

(async () => {
  let callCount = 0;
  const addBtn = {
    dataset: {},
    disabled: false,
    textContent: "➕ 追加",
    classList: {
      contains: (cls) => cls === "added" && addBtn.textContent === "✓ 追加済"
    }
  };

  async function clickHandler() {
    if (addBtn.dataset.inFlight === "true") return;
    addBtn.dataset.inFlight = "true";
    addBtn.disabled = true;
    addBtn.textContent = "追加中...";
    try {
      callCount++;
      await new Promise(r => setTimeout(r, 20));
      // Simulate failure
      addBtn.textContent = "再試行";
      addBtn.disabled = false;
      setTimeout(() => {
        if (!addBtn.classList.contains("added")) {
          addBtn.textContent = "➕ 追加";
        }
      }, 50);
    } finally {
      delete addBtn.dataset.inFlight;
    }
  }

  // Concurrent clicks while in flight
  const p1 = clickHandler();
  const p2 = clickHandler();
  await Promise.all([p1, p2]);

  // Second click must have been ignored by inFlight guard
  assert.strictEqual(callCount, 1);
  assert.strictEqual(addBtn.dataset.inFlight, undefined);
  assert.strictEqual(addBtn.textContent, "再試行");

  // Timer reverts to "➕ 追加"
  await new Promise(r => setTimeout(r, 70));
  assert.strictEqual(addBtn.textContent, "➕ 追加");

  console.log("ISSUE-07 PASS");
})();
"""
    output = _run_node(node_test)
    assert "ISSUE-07 PASS" in output
