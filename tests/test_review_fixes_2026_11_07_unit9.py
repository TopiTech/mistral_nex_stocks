"""Unit 9 (CI/lint/test-hardening) regression tests for the 2026-11-07 review.

Covers:
* M31/L33 — restrictive rotating file handlers stay 0o600 across rollover
  (backend logging_config.py and native_host/native_host.py).
* M36 — the ESLint config enables the recommended base and declares the
  @eslint/js devDependency; the classic-script rule overrides stay
  documented and intentional.
"""

from __future__ import annotations

import ast
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rollover_and_assert_chmod_600(handler_cls) -> None:
    """Rolled-over live files must be re-chmod-ed to 0o600 (M31/L33)."""
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "app.log"

        logger = logging.getLogger("m31.test")
        logger.setLevel(logging.DEBUG)
        chmod_calls: list[tuple[str, int]] = []
        real_chmod = os.chmod

        def _spy(path, mode, *, dir_fd=None, follow_symlinks=True):
            chmod_calls.append((str(path), int(mode)))
            return real_chmod(
                path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks
            )

        with patch.object(os, "chmod", _spy):
            # maxBytes=1 with delay=False: the file opens at construction and
            # EVERY subsequent emit forces doRollover — the exact production
            # path that used to reset the mode to the process umask.
            handler = handler_cls(
                str(log_path),
                maxBytes=1,
                backupCount=1,
                encoding="utf-8",
            )
            logger.addHandler(handler)
            try:
                # First emit writes a record (the construction-time open left
                # the file empty, so no rollover happens yet).
                logger.info("first message")
                assert (str(log_path), 0o600) in chmod_calls

                chmod_calls.clear()
                # Second emit exceeds maxBytes -> doRollover -> _open().
                logger.info("second message")
                assert (str(log_path), 0o600) in chmod_calls, (
                    "rollover re-opened the live log without re-asserting 0o600"
                )
            finally:
                logger.removeHandler(handler)
                handler.close()


def test_backend_rotating_handlers_keep_0600_across_rollover() -> None:
    from logging_config import _RestrictiveRotatingFileHandler

    _rollover_and_assert_chmod_600(_RestrictiveRotatingFileHandler)


def test_native_host_rotating_handler_keeps_0600_across_rollover() -> None:
    """Native-host log file must stay 0o600 across rollover (L33).

    The handler class is extracted via ast so importing native_host.py's
    module-level side effects (native-messaging protocol setup, backend
    spawn) never runs inside the test process.
    """
    import logging.handlers

    source = (ROOT / "native_host" / "native_host.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_node = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "_RestrictiveRotatingFileHandler":
            class_node = node
            break
    assert class_node is not None, "native_host must define _RestrictiveRotatingFileHandler"

    namespace = {
        "logging": logging,
        "os": os,
        "RotatingFileHandler": logging.handlers.RotatingFileHandler,
    }
    exec(compile(ast.Module(body=[class_node], type_ignores=[]), "<ast>", "exec"), namespace)  # noqa: S102 - test-local extraction
    handler_cls = namespace["_RestrictiveRotatingFileHandler"]
    _rollover_and_assert_chmod_600(handler_cls)


def test_native_host_source_uses_restrictive_handler_for_log() -> None:
    source = (ROOT / "native_host" / "native_host.py").read_text(encoding="utf-8")
    assert "_file_handler = _RestrictiveRotatingFileHandler(" in source


def test_eslint_config_enables_recommended_base() -> None:
    """M36: js.configs.recommended must stay active with documented overrides."""
    source = (ROOT / "eslint.config.mjs").read_text(encoding="utf-8")
    assert 'import js from "@eslint/js";' in source
    assert "js.configs.recommended," in source
    # Classic-script architecture: sibling-file globals are not redeclarations.
    assert '"no-redeclare": ["error", { builtinGlobals: false }]' in source
    # Documented no-op catch pattern.
    assert '"no-empty": ["error", { allowEmptyCatch: true }]' in source


def test_eslint_deps_pin_js_configs_package() -> None:
    """M36: the npm build must declare the package the config imports."""
    import json

    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert "@eslint/js" in pkg.get("devDependencies", {}), (
        "eslint.config.mjs imports @eslint/js — it must be a declared devDependency"
    )
