"""Regression tests for Unit 4: theme color polarity and Settings keyboard accessibility."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent


def _read(rel_path: str) -> str:
    return (ROOT_DIR / rel_path).read_text(encoding="utf-8")


def _run_node(script: str, expected_marker: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for JavaScript unit tests")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT_DIR,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert result.stdout.strip().endswith(expected_marker), (
        f"Expected {expected_marker}, got STDOUT={result.stdout!r} STDERR={result.stderr!r}"
    )


def test_experimental_orbit_text_pos_neg_follow_theme_tokens():
    css = _read("static/css/experimental-orbit.css")
    pos_match = re.search(r"\.text-pos\s*\{([^}]*)\}", css)
    neg_match = re.search(r"\.text-neg\s*\{([^}]*)\}", css)
    assert pos_match is not None, ".text-pos selector missing in experimental-orbit.css"
    assert neg_match is not None, ".text-neg selector missing in experimental-orbit.css"
    assert "var(--color-positive" in pos_match.group(1)
    assert "var(--color-negative" in neg_match.group(1)

    colors_css = _read("static/css/colors.css")
    assert "--color-positive:" in colors_css
    assert "--color-negative:" in colors_css


def test_settings_css_focus_rings_for_provider_toggle_and_model_chip():
    css = _read("static/css/settings.css")
    assert ".provider-toggle-label:focus-within .provider-toggle-card" in css
    assert '.provider-toggle-label input[type="radio"]:focus-visible + .provider-toggle-card' in css
    assert ".model-chip:focus-visible" in css
    chip_match = re.search(r"\.model-chip\s*\{([^}]*)\}", css)
    assert chip_match is not None
    assert "appearance: none;" in chip_match.group(1)
    assert "font: inherit;" in chip_match.group(1)


def test_settings_discovered_model_chips_render_as_native_buttons():
    node_script = r"""
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let __finished = false;
process.on("beforeExit", () => {
  if (!__finished) {
    console.error("Node exited before async test completed");
    process.exit(1);
  }
});

function makeEl(tag = "div") {
  const listeners = new Map();
  const children = [];
  const el = {
    tagName: String(tag).toUpperCase(),
    type: "",
    className: "",
    textContent: "",
    value: "",
    disabled: false,
    hidden: false,
    dataset: {},
    style: {},
    children,
    setAttribute() {},
    removeAttribute() {},
    appendChild(child) { children.push(child); return child; },
    replaceChildren(...nodes) { children.length = 0; children.push(...nodes); },
    addEventListener(ev, fn) {
      if (!listeners.has(ev)) listeners.set(ev, []);
      listeners.get(ev).push(fn);
    },
    dispatchEvent(ev, obj = {}) {
      const list = listeners.get(ev) || [];
      return Promise.all(list.map((fn) => fn({ preventDefault() {}, stopPropagation() {}, ...obj })));
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  return el;
}

(async () => {
  const settingsSrc = fs.readFileSync(
    path.join(process.cwd(), "static/js/settings.js"),
    "utf8",
  );
  const domListeners = [];
  const verifyBtn = makeEl("button");
  const verifyStatus = makeEl("div");
  const discoveredBox = makeEl("div");
  const modelInput = makeEl("input");
  const baseUrlInput = makeEl("input");
  baseUrlInput.value = "http://localhost:11434/v1";
  const els = {
    "verify-openai-btn": verifyBtn,
    "openai-verify-status": verifyStatus,
    "openai-discovered-models": discoveredBox,
    "openai-model-input": modelInput,
    "openai-base-url-input": baseUrlInput,
  };
  const ctx = {
    document: {
      addEventListener: (ev, fn) => { if (ev === "DOMContentLoaded") domListeners.push(fn); },
      getElementById: (id) => els[id] || null,
      querySelectorAll: () => [],
      querySelector: () => null,
      createElement: (tag) => makeEl(tag),
      createTextNode: (t) => ({ nodeType: 3, textContent: String(t ?? "") }),
    },
    window: {},
    localStorage: { getItem: () => null, setItem: () => {} },
    navigator: {},
    setTimeout: () => 1,
    clearTimeout: () => {},
    getColorSchemePreference: () => "western",
    initThemeColorScheme: () => {},
    showSettingsMessage: () => {},
    setSafeText: (e, t) => { if (e) e.textContent = String(t ?? ""); },
    CONSTANTS: { refreshColors: () => {} },
    fetch: async () => ({
      ok: true,
      status: 200,
      text: async () => "{}",
      json: async () => ({ ok: true, stocks: { us: [], jp: [] } }),
    }),
    csrfFetch: async (url) => {
      if (String(url).includes("/api/credentials/verify")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            ok: true,
            message: "接続成功",
            models: ["llama3.3:latest", "gpt-4o-mini"],
          }),
        };
      }
      return { ok: true, status: 200, json: async () => ({ ok: true }) };
    },
    logger: { error: () => {}, warn: () => {}, debug: () => {} },
    showToast: () => {},
    AbortController,
    Promise,
    String,
    Number,
    Array,
    Object,
    Error,
    console,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.runInNewContext(settingsSrc, ctx);
  domListeners.forEach((fn) => fn());

  await verifyBtn.dispatchEvent("click");
  const chips = discoveredBox.children.filter((el) => el.className === "model-chip");
  assert.strictEqual(chips.length, 2, "Expected two model chips");
  for (const chip of chips) {
    assert.strictEqual(chip.tagName, "BUTTON", "Model chip must be a native <button>");
    assert.strictEqual(chip.type, "button", "Model chip button must set type='button'");
  }
  await chips[1].dispatchEvent("click");
  assert.strictEqual(modelInput.value, "gpt-4o-mini");

  __finished = true;
  console.log("UNIT4 A11Y PASS");
})().catch((err) => { console.error(err); process.exit(1); });
"""
    _run_node(node_script, "UNIT4 A11Y PASS")
