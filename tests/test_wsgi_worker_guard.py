"""Regression tests for H1: wsgi.py must reject multi-worker mode.

gunicorn controls the worker count via the WEB_CONCURRENCY environment variable.
These tests verify that wsgi.py detects WEB_CONCURRENCY > 1 and aborts.
"""

import importlib
import pytest
from unittest.mock import patch
import wsgi as wsgi_mod


def test_single_worker_is_allowed():
    """WEB_CONCURRENCY=1 (or unset) must not raise."""
    with patch.dict("os.environ", {"WEB_CONCURRENCY": "1", "MNS_WORKER_VALIDATION": "1"}):
        importlib.reload(wsgi_mod)


def test_multi_worker_is_rejected():
    """WEB_CONCURRENCY=4 must abort startup (single-worker only)."""
    with patch.dict("os.environ", {"WEB_CONCURRENCY": "4", "MNS_WORKER_VALIDATION": "1"}):
        with pytest.raises(SystemExit) as excinfo:
            importlib.reload(wsgi_mod)
        assert excinfo.value.code == 1


def test_validation_can_be_disabled():
    """MNS_WORKER_VALIDATION=0 disables the guard even with WEB_CONCURRENCY=4."""
    with patch.dict("os.environ", {"WEB_CONCURRENCY": "4", "MNS_WORKER_VALIDATION": "0"}):
        importlib.reload(wsgi_mod)
