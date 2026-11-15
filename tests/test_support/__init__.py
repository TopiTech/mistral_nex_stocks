"""Test support package providing modular utilities, mocks, and fixtures."""

from __future__ import annotations

from tests.test_support.environment import (
    apply_baseline_test_env,
    get_baseline_test_env,
    require_node,
    run_node_script,
)
from tests.test_support.executors import SynchronousExecutor
from tests.test_support.keyring_mock import MemoryKeyring, get_shared_memory_keyring
from tests.test_support.patches import (
    DEFAULT_STUB_PUBLIC_HOSTS,
    apply_all_test_patches,
    apply_background_stubs,
    apply_storage_and_config_patches,
    apply_synchronous_executors,
    apply_yfinance_cache_isolation,
    stub_public_outbound_dns,
)
from tests.test_support.state_reset import reset_app_state_internals
from tests.test_support.temp_config import cleanup_temp_files, create_temp_config, safe_temp_dir

__all__ = [
    "DEFAULT_STUB_PUBLIC_HOSTS",
    "MemoryKeyring",
    "SynchronousExecutor",
    "apply_all_test_patches",
    "apply_background_stubs",
    "apply_baseline_test_env",
    "apply_storage_and_config_patches",
    "apply_synchronous_executors",
    "apply_yfinance_cache_isolation",
    "cleanup_temp_files",
    "create_temp_config",
    "get_baseline_test_env",
    "get_shared_memory_keyring",
    "require_node",
    "reset_app_state_internals",
    "run_node_script",
    "safe_temp_dir",
    "stub_public_outbound_dns",
]
