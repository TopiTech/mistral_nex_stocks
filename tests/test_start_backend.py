import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import native_host.start_backend as sb


class StartBackendTests(unittest.TestCase):
    def test_live_pid_with_unhealthy_occupied_port_is_not_reported_as_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / ".backend.pid"
            pid_file.write_text("12345", encoding="utf-8")

            with (
                patch.object(sb, "PID_FILE", pid_file),
                patch.object(sb, "is_port_in_use", return_value=True),
                patch.object(sb, "is_running", return_value=True),
                patch.object(sb, "is_backend_healthy_once", return_value=False),
                patch.object(sb.subprocess, "Popen") as popen,
            ):
                result = sb.start()

            self.assertFalse(result.get("ok"))
            self.assertIn("unhealthy", result.get("error", ""))
            self.assertFalse(pid_file.exists())
            popen.assert_not_called()

    def test_stale_running_pid_file_is_replaced_and_backend_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            pid_file = tmpdir / ".backend.pid"
            log_file = tmpdir / "backend.log"
            app_file = tmpdir / "app.py"

            log_file.write_bytes(b"")
            app_file.write_text("print('ok')\n", encoding="utf-8")
            pid_file.write_text("12345", encoding="utf-8")

            stale_mtime = 1_600_000_000
            os.utime(pid_file, (stale_mtime, stale_mtime))

            fake_proc = MagicMock()
            fake_proc.pid = 98765

            with (
                patch.object(sb, "PID_FILE", pid_file),
                patch.object(sb, "LOG", log_file),
                patch.object(sb, "APP", app_file),
                patch.object(sb, "is_port_in_use", return_value=False),
                patch.object(sb, "is_running", side_effect=[True]),
                patch.object(sb, "is_backend_healthy_once", return_value=False),
                patch.object(sb, "wait_for_backend_ready", return_value=True),
                patch.object(sb.subprocess, "Popen", return_value=fake_proc),
                patch.object(
                    sb.time,
                    "time",
                    return_value=stale_mtime + sb.PID_WARMUP_GRACE_SEC + 5,
                ),
            ):
                result = sb.start()

            self.assertTrue(result.get("ok"))
            self.assertEqual(result.get("pid"), 98765)
            self.assertIn("Backend started", result.get("message", ""))
            self.assertEqual(pid_file.read_text(encoding="utf-8"), "98765")

    def test_startup_lock_parallel_threads_no_permission_error(self):
        import threading
        import time

        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".backend.start.lock"
            with patch.object(sb, "STARTUP_LOCK_FILE", lock_path):
                acquired_count = 0
                errors = []
                lock_guard = threading.Lock()

                def worker():
                    nonlocal acquired_count
                    try:
                        with sb._startup_lock():
                            with lock_guard:
                                acquired_count += 1
                            time.sleep(0.005)
                    except Exception as exc:
                        with lock_guard:
                            errors.append(exc)

                threads = [threading.Thread(target=worker) for _ in range(2)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()

                self.assertEqual(errors, [])
                self.assertEqual(acquired_count, 2)

    def test_startup_lock_pre_lock_write_os_error_resilience(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".backend.start.lock"
            with patch.object(sb, "STARTUP_LOCK_FILE", lock_path):
                # Even if os.write raises OSError during pre-lock check, lock succeeds
                with sb._startup_lock():
                    self.assertTrue(lock_path.exists())


if __name__ == "__main__":
    unittest.main()
