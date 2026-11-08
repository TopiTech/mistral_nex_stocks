"""Regression test for the native host's PID-file staging cleanup.

``_start`` writes the spawned backend PID to ``.backend.tmp`` and promotes it
with ``os.replace``. When the replace fails (permission error, AV scanner,
read-only runtime directory) the code falls back to a direct write; before the
fix the staging file stayed behind on every failed promotion.
"""

from pathlib import Path
from types import SimpleNamespace

from native_host import start_backend


def test_failed_pid_replace_removes_staging_tmp_file(tmp_path: Path, monkeypatch) -> None:
    pid_file = tmp_path / ".backend.pid"
    staging = tmp_path / ".backend.tmp"
    monkeypatch.setattr(start_backend, "PID_FILE", pid_file)
    monkeypatch.setattr(start_backend, "_LEGACY_PID_FILE", tmp_path / "legacy.pid")
    monkeypatch.setattr(start_backend, "is_port_in_use", lambda port: False)
    monkeypatch.setattr(
        start_backend.subprocess,
        "Popen",
        lambda *args, **kwargs: SimpleNamespace(pid=12345),
    )
    monkeypatch.setattr(start_backend, "wait_for_backend_ready", lambda timeout_sec=2.0: False)
    monkeypatch.setattr(start_backend, "is_running", lambda pid: True)

    def failing_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(start_backend.os, "replace", failing_replace)

    result = start_backend._start()

    assert result["ok"] is True
    assert result.get("warming_up") is True
    assert pid_file.read_text(encoding="utf-8") == "12345"
    assert not staging.exists()
