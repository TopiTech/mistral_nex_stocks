import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import native_host.start_backend as sb


class StartBackendTests(unittest.TestCase):
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

            fake_proc = MagicMock()
            fake_proc.pid = 98765

            with patch.object(sb, "PID_FILE", pid_file), \
                patch.object(sb, "LOG", log_file), \
                patch.object(sb, "APP", app_file), \
                patch.object(sb, "is_port_in_use", return_value=False), \
                patch.object(sb, "is_running", side_effect=[True]), \
                patch.object(sb, "is_backend_healthy_once", return_value=False), \
                patch.object(sb, "wait_for_backend_ready", return_value=True), \
                patch.object(sb.subprocess, "Popen", return_value=fake_proc), \
                patch.object(sb.time, "time", return_value=stale_mtime + sb.PID_WARMUP_GRACE_SEC + 5), \
                patch.object(Path, "stat", return_value=MagicMock(st_mtime=stale_mtime)):
                result = sb.start()

            self.assertTrue(result.get("ok"))
            self.assertEqual(result.get("pid"), 98765)
            self.assertIn("Backend started", result.get("message", ""))
            self.assertEqual(pid_file.read_text(encoding="utf-8"), "98765")


if __name__ == "__main__":
    unittest.main()
