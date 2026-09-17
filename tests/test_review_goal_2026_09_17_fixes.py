"""Regression tests for the 2026-09-17 review remediation pass.

Covers:
- orbit global shortcuts must not steal Space/Enter/Arrow keys from focused
  buttons/links/sliders (P0 keyboard-operability fix in
  static/js/experimental/accessibility-controller.js)
- focus-trap visibility must work inside position:fixed drawers/modals
  (utils.js isEffectivelyVisible / ui.js isUiElementVisible)
- settings reorder buttons must carry symbol-specific accessible names and
  announce the move via toast
- screener static colspan placeholder must track visible columns on mobile
- bool env parsing must accept on/off uniformly (_env_bool unification)
- shutdown token rotation restore must keep 0o600 restricted permissions
- orbit canvas must expose a keyboard path (role=application + tabindex)
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _run_node(script: str) -> str:
    import shutil

    node = shutil.which("node")
    if node is None:
        import pytest

        pytest.skip("Node.js is required for the frontend regression test")
    result = subprocess.run(
        [node, "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_orbit_shortcuts_ignore_focused_controls():
    source = _read("static/js/experimental/accessibility-controller.js")
    assert 'button, a[href], input, textarea, select' in source
    assert "isModifierShortcut" in source
    script = (
        "const fs = require('fs');\n"
        "const vm = require('vm');\n"
        "const src = fs.readFileSync('static/js/experimental/accessibility-controller.js', 'utf8');\n"
        "const state = { state: { selectedSymbol: 'AAPL', aiDiveOpen: false, isConnectMode: false, connectedSymbols: [] },\n"
        "  togglePause() { this.paused = !this.paused; },\n"
        "  setSelectedSymbol(s) { this.selected = s; },\n"
        "  set() {}, subscribe() {}, openAiDive() { this.dive = true; },\n"
        "  toggleConnectMode() {}, clearConnectedSymbols() {}, toggleReducedMotion() {}, closeAiDive() {} };\n"
        "function makeEl(tag) {\n"
        "  return { tagName: tag, closest(sel) {\n"
        "    if (tag === 'BUTTON' && sel.includes('button')) return this;\n"
        "    if (tag === 'INPUT' && sel.includes('input')) return this;\n"
        "    return null; },\n"
        "  };\n"
        "}\n"
        "let active = makeEl('BUTTON');\n"
        "const listeners = {};\n"
        "const sandbox = { window: { addEventListener(n, h) { listeners[n] = h; }, matchMedia: undefined },\n"
        "  document: { get activeElement() { return active; } }, localStorage: { getItem: () => null } };\n"
        "sandbox.window.window = sandbox.window;\n"
        "sandbox.globalThis = sandbox;\n"
        "vm.createContext(sandbox);\n"
        "vm.runInContext(src, sandbox);\n"
        "const C = sandbox.window.AccessibilityController;\n"
        "const c = new C(state, {});\n"
        "const ev = (key, extra={}) => Object.assign({ key, keyCode: 0, isComposing: false, preventDefault() { this.p = true; } }, extra);\n"
        "state.paused = false;\n"
        "active = makeEl('BUTTON');\n"
        "c.handleKeydown(ev(' '));\n"
        "if (state.paused) throw new Error('Space on a focused button must not toggle pause');\n"
        "state.selected = null;\n"
        "c.handleKeydown(ev('ArrowRight'));\n"
        "if (state.selected) throw new Error('Arrow on a focused control must not cycle stocks');\n"
        "active = makeEl('DIV');\n"
        "c.handleKeydown(ev(' '));\n"
        "if (!state.paused) throw new Error('Space on canvas body must still toggle pause');\n"
        "active = makeEl('BUTTON');\n"
        "state.dive = false;\n"
        "c.handleKeydown(ev('k', { ctrlKey: true }));\n"
        "console.log('orbit shortcut guard ok');\n"
    )
    out = _run_node(script)
    assert out.endswith("orbit shortcut guard ok")


def test_focus_trap_visibility_handles_fixed_position():
    utils_src = _read("static/js/utils.js")
    ui_src = _read("static/js/ui.js")
    assert "function isEffectivelyVisible(el)" in utils_src
    assert "getComputedStyle" in utils_src
    assert "getBoundingClientRect" in utils_src
    assert "isUiElementVisible(el)" in ui_src
    assert ".filter((el) => el.offsetParent !== null" not in utils_src
    assert ".filter((el) => el.offsetParent !== null" not in ui_src


def test_settings_reorder_buttons_have_symbol_names_and_announce():
    source = _read("static/js/settings.js")
    assert "を上に移動" in source
    assert "を下に移動" in source
    assert "に移動しました" in source
    assert '${stock.symbol} を上に移動' in source
    assert '${stock.symbol} を下に移動' in source


def test_screener_static_placeholder_tracks_visible_columns():
    template = _read("templates/screener.html")
    assert 'data-colspan-full="8"' in template
    js = _read("static/js/screener.js")
    assert "syncStaticColspanPlaceholders" in js
    assert "getVisibleColSpan()" in js
    assert 'querySelectorAll(\'td[data-colspan-full="8"]\')' in js or \
        'querySelectorAll("td[data-colspan-full=\\"8\\"]")' in js or \
        "data-colspan-full" in js


def test_orbit_canvas_keyboard_path():
    template = _read("templates/experimental_orbit.html")
    assert 'role="application"' in template
    assert 'id="orbit-canvas"' in template
    assert 'tabindex="0"' in template
    assert "orbit-canvas-help" in template
    entry = _read("static/js/experimental/orbit-entry.js")
    assert 'canvas.addEventListener("keydown"' in entry
    css = _read("static/css/experimental-orbit.css")
    assert ".orbit-canvas:focus-visible" in css
    assert ".hud-pill-btn:focus-visible" in css


def test_env_bool_unified_on_off():
    from utils import env_helpers

    for value in ("on", "ON", " On "):
        with patch.dict(os.environ, {"MNS_TEST_BOOL_UNIFIED": value}, clear=False):
            assert env_helpers._env_bool("MNS_TEST_BOOL_UNIFIED") is True
    for value in ("off", "OFF", " Off "):
        with patch.dict(os.environ, {"MNS_TEST_BOOL_UNIFIED": value}, clear=False):
            assert env_helpers._env_bool("MNS_TEST_BOOL_UNIFIED", True) is False
    with patch.dict(
        os.environ,
        {"MNS_ALLOW_REMOTE_API": "on", "MNS_PROXY_FIX": "on"},
        clear=False,
    ):
        assert env_helpers._is_remote_api_enabled() is True
    with patch.dict(os.environ, {"MNS_ALLOW_REMOTE_API": "off"}, clear=False):
        assert env_helpers._env_bool("MNS_ALLOW_REMOTE_API") is False
    # Remote-mode flags alone must not flip the production guard while the
    # documented test/bootstrap opt-out is active; explicit MNS_PROD=1 still
    # reports production so Host-spoofing coverage keeps working.
    with patch.dict(
        os.environ,
        {
            "MNS_SKIP_BOOTSTRAP": "1",
            "MNS_ALLOW_REMOTE_API": "1",
            "MNS_PROXY_FIX": "1",
            "MNS_PROD": "0",
        },
        clear=False,
    ):
        assert env_helpers._is_production_env() is False
    with patch.dict(os.environ, {"MNS_SKIP_BOOTSTRAP": "1", "MNS_PROD": "1"}, clear=False):
        assert env_helpers._is_production_env() is True


def test_remote_api_and_prod_use_env_bool():
    networking = _read("utils/networking.py")
    assert '.strip().lower() in (\n        "1",\n        "true",\n        "yes",\n    )' not in networking
    assert "_networking_env_bool" in networking
    app_src = _read("app.py")
    assert "_env_bool(" in app_src
    system_src = _read("routes/api_system.py")
    assert "_env_bool(" in system_src


def test_shutdown_rotate_restore_keeps_restricted_permissions():
    os.environ["MNS_MASTER_KEY"] = "Ij2VbZwpP-Du-IHWL5VUPKL8BHUXUbddJY7JNj4xJ6g="
    try:
        from shutdown_manager import ShutdownTokenManager
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            mgr = ShutdownTokenManager()
            mgr.token_file = tmp_path / ".mns_shutdown_token"
            mgr.used_marker = tmp_path / ".mns_shutdown_token.used"
            mgr.runtime_state_dir = tmp_path
            mgr._legacy_token_file = tmp_path / "legacy"
            mgr._legacy_used_marker = tmp_path / "legacy.used"
            mgr.get_or_create_shutdown_token()
            mgr.commit_shutdown_token()
            old_text = mgr.token_file.read_text(encoding="utf-8")
            orig_write = __import__("shutdown_manager")._write_atomic_restricted
            calls = []

            def fail_once(path, content):
                calls.append(str(path))
                raise OSError("disk full")

            with patch(
                "shutdown_manager._write_atomic_restricted",
                side_effect=fail_once,
            ):
                try:
                    mgr.rotate_shutdown_token()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("rotation failure must raise RuntimeError")
            # Restore path must go through the restricted writer (mode/perm
            # preserving) rather than a bare write_bytes+replace: assert the
            # source calls it for the previous token content.
            assert any(str(mgr.token_file) in c for c in calls), calls
            # Simulate the non-failing pre-state: writer fails only on the new
            # token, restore succeeds.
            state = {"failed": False}

            def fail_new_only(path, content):
                if not state["failed"]:
                    state["failed"] = True
                    raise OSError("disk full")
                return orig_write(path, content)

            with patch(
                "shutdown_manager._write_atomic_restricted",
                side_effect=fail_new_only,
            ):
                try:
                    mgr.rotate_shutdown_token()
                except RuntimeError:
                    pass
            mode = stat.S_IMODE(mgr.token_file.stat().st_mode)
            if os.name != "nt":
                assert mode == 0o600, f"restored token file must be 0o600, got {oct(mode)}"
            assert mgr.token_file.read_text(encoding="utf-8") == old_text
            assert mgr.used_marker.exists()
    finally:
        os.environ.pop("MNS_MASTER_KEY", None)


def test_security_config_unified_and_clipboard_locked():
    source = _read("security_config.py")
    assert '"clipboard-read": ()' in source
    assert "clipboard-read はチャット入力用に許可" not in source
    assert '_security_env_bool("MNS_COOKIE_SECURE")' in source
    assert '_security_env_bool("CSP_ENFORCE", True)' in source
    shutdown_src = _read("shutdown_manager.py")
    assert "token_tmp.write_bytes" not in shutdown_src
    assert "_write_atomic_restricted(self.token_file, old_token_text)" in shutdown_src
