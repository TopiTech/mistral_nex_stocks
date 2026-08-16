#!/usr/bin/env python3
"""バックエンドプロセス起動管理モジュール"""

import logging
import os
import socket
import subprocess  # nosec B404
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import psutil
import requests

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
# R1: Default backend log/PID/startup-lock to per-user runtime data dir.
try:
    from config_store import APP_DATA_DIR as _APP_DATA_DIR  # type: ignore
    _STATE_DIR = _APP_DATA_DIR
except Exception:  # pragma: no cover - defensive import fallback
    _STATE_DIR = ROOT
try:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
LOG = _STATE_DIR / "backend.log"
PID_FILE = _STATE_DIR / ".backend.pid"
_LEGACY_PID_FILE = ROOT / ".backend.pid"
STARTUP_LOCK_FILE = _STATE_DIR / ".backend.start.lock"
_LEGACY_STARTUP_LOCK_FILE = ROOT / ".backend.start.lock"
PID_WARMUP_GRACE_SEC = 120
DEFAULT_BACKEND_PORT = 5000
MIN_BACKEND_PORT = 1
MAX_BACKEND_PORT = 65535
EXPECTED_HEALTH_APP = "Mistral NeX Stocks"

logger = logging.getLogger("native_host.start_backend")
if not logger.handlers:
    file_handler = logging.FileHandler(LOG, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(file_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def _is_expected_backend_response(response: requests.Response) -> bool:
    """Return whether a health response identifies a ready application instance."""
    if not 200 <= int(getattr(response, "status_code", 0)) < 300:
        return False
    try:
        data = response.json()
    except (ValueError, TypeError):
        return False
    return (
        isinstance(data, dict)
        and data.get("ok") is True
        and data.get("app") == EXPECTED_HEALTH_APP
        and data.get("ready") is True
    )


def get_backend_port() -> int:
    """バックエンドポート番号を環境変数から取得

    本体側 (constants.BACKEND_PORT) と同じ 1..65535 の範囲へ補正する。
    範囲外・非数値の設定は既定ポートへフォールバックし、socket 接続確認で
    OverflowError を起こさないようにする。
    """
    port_text = os.environ.get("MNS_BACKEND_PORT", "").strip()
    if port_text:
        try:
            port = int(port_text)
        except ValueError:
            port = 0
        if MIN_BACKEND_PORT <= port <= MAX_BACKEND_PORT:
            return port
        logger.warning(
            "Invalid MNS_BACKEND_PORT value %r (must be %d..%d); falling back to default %s",
            port_text,
            MIN_BACKEND_PORT,
            MAX_BACKEND_PORT,
            DEFAULT_BACKEND_PORT,
        )
    return DEFAULT_BACKEND_PORT


def is_port_in_use(port: int) -> bool:
    """指定ポートが使用中か確認"""
    for host in ("127.0.0.1", "localhost"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
    return False


def is_running(pid: int) -> bool:
    """PIDが実行中か確認"""
    if pid <= 0:
        return False
    try:
        proc = psutil.Process(pid)
        # Filters out zombie and dead process states
        if proc.status() in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
            return False
        return proc.is_running()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # Access denied indicates the process exists and is running under other credentials
        return True
    except Exception as exc:
        logger.debug("is_running check failed for pid=%s: %s", pid, exc)
        return False


def wait_for_backend_ready(timeout_sec: float = 20.0) -> bool:  # 個人利用向けに最適化
    """バックエンドのヘルスチェックが通るまで待機"""
    deadline = time.time() + timeout_sec
    port = get_backend_port()
    health_urls = [
        f"http://127.0.0.1:{port}/api/health",
        f"http://localhost:{port}/api/health",
    ]
    while time.time() < deadline:
        for url in health_urls:
            try:
                # Use requests for health checks to avoid unsafe urlopen patterns flagged by security linters
                resp = requests.get(url, headers={"Cache-Control": "no-store"}, timeout=1.5)
                try:
                    if _is_expected_backend_response(resp):
                        return True
                finally:
                    try:
                        resp.close()
                    except Exception:
                        logger.debug("Failed to close health check response")
            except (requests.RequestException, OSError, ValueError) as exc:
                logger.debug("Health check request failed url=%s: %s", url, exc)
        time.sleep(0.35)
    return False


def is_backend_healthy_once(timeout_sec: float = 1.5) -> bool:
    """バックエンドのヘルスチェックを1回だけ実行"""
    port = get_backend_port()
    health_urls = [
        f"http://127.0.0.1:{port}/api/health",
        f"http://localhost:{port}/api/health",
    ]
    for url in health_urls:
        try:
            resp = requests.get(url, headers={"Cache-Control": "no-store"}, timeout=timeout_sec)
            try:
                if _is_expected_backend_response(resp):
                    return True
            finally:
                try:
                    resp.close()
                except Exception:
                    logger.debug("Failed to close health check response (once)")
        except requests.RequestException:
            continue
    return False


@contextmanager
def _startup_lock():
    """Serialize startup check/spawn/PID update across native-host processes."""
    STARTUP_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import msvcrt

        msvcrt_module = cast(Any, msvcrt)
        fd = os.open(str(STARTUP_LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
        locked = False
        try:
            if os.fstat(fd).st_size == 0:
                try:
                    os.write(fd, b"L")
                    os.lseek(fd, 0, os.SEEK_SET)
                except OSError:
                    pass
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt_module.locking(fd, msvcrt_module.LK_LOCK, 1)
            locked = True
            if os.fstat(fd).st_size == 0:
                try:
                    os.write(fd, b"L")
                    os.lseek(fd, 0, os.SEEK_SET)
                except OSError:
                    pass
            yield
        finally:
            if locked:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt_module.locking(fd, msvcrt_module.LK_UNLCK, 1)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
    else:
        import fcntl

        with STARTUP_LOCK_FILE.open("a+", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def _start(extension_id=None):
    """バックエンドプロセスを起動または既存起動を確認"""
    # 環境変数で起動元拡張機能のオリジンをバックエンドに伝える
    env = os.environ.copy()
    port = get_backend_port()
    if isinstance(extension_id, str):
        extension_id = extension_id.strip()
        if len(extension_id) == 32 and extension_id.isalnum():
            env["MNS_EXTENSION_ORIGIN"] = f"chrome-extension://{extension_id}"
        else:
            logger.warning("Invalid extensionId passed to start_backend: %r", extension_id)
    # 実際に応答があるかどうかも含めて判定
    port_in_use = is_port_in_use(port)

    # R1: Honor a legacy project-root PID file when present, so an instance
    # started by an older version of the native host can still be detected.
    legacy_pid_text: str | None = None
    try:
        if not PID_FILE.exists() and _LEGACY_PID_FILE.exists():
            legacy_pid_text = _LEGACY_PID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        legacy_pid_text = None

    pid_source: Path | None
    if PID_FILE.exists():
        pid_source = PID_FILE
    elif legacy_pid_text is not None:
        pid_source = _LEGACY_PID_FILE
    else:
        pid_source = None
    if pid_source is not None:
        try:
            pid_text = pid_source.read_text(encoding="utf-8").strip()
            if pid_text:
                pid = int(pid_text)
                if is_running(pid):
                    healthy = is_backend_healthy_once(timeout_sec=1.5)
                    if healthy:
                        return {
                            "ok": True,
                            "message": f"Already running (pid={pid})",
                            "pid": pid,
                            "port": port,
                        }
                    # A live PID is not proof that it owns the backend. In
                    # particular, accepting a non-healthy listener as ours
                    # makes the extension report success while all API calls
                    # are routed to a different or wedged process.
                    if port_in_use:
                        logger.warning(
                            "Backend port %s is occupied but health check failed; "
                            "PID %s is not accepted as a healthy backend.",
                            port,
                            pid,
                        )
                        PID_FILE.unlink(missing_ok=True)
                        return {
                            "ok": False,
                            "error": f"Port {port} is already in use by an unhealthy process.",
                            "port": port,
                        }
                    # PID が生きていてもヘルス応答が長時間得られない場合は
                    # PID再利用や別プロセス混入を疑い、古いPID情報として破棄する。
                    pid_file_age_sec = max(0.0, time.time() - pid_source.stat().st_mtime)
                    if pid_file_age_sec > PID_WARMUP_GRACE_SEC:
                        logger.warning(
                            "Stale backend PID detected (pid=%s age=%.1fs). Removing pid file.",
                            pid,
                            pid_file_age_sec,
                        )
                        pid_source.unlink(missing_ok=True)
                    else:
                        return {
                            "ok": True,
                            "message": (
                                f"Backend process is still starting (pid={pid});"
                                " waiting for health check."
                            ),
                            "pid": pid,
                            "port": port,
                            "warming_up": True,
                        }
            # 実行中でない場合は古いPIDファイルを削除
            pid_source.unlink(missing_ok=True)
        except (OSError, ValueError):
            logger.warning("Failed to read/cleanup stale pid file: %s", pid_source, exc_info=True)

    if port_in_use:
        if is_backend_healthy_once(timeout_sec=1.5):
            return {
                "ok": True,
                "message": f"Already running (detected healthy backend on port {port})",
                "pid": None,
                "port": port,
                "detected_by_health": True,
            }
        return {
            "ok": False,
            "error": f"Port {port} is already in use by another process.",
            "port": port,
        }

    python_exe = sys.executable or "python"
    if os.name == "nt" and python_exe.lower().endswith("python.exe"):
        pythonw_cand = Path(python_exe).with_name("pythonw.exe")
        if pythonw_cand.exists():
            python_exe = str(pythonw_cand)

    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":  # pragma: no cover
        # DETACHED_PROCESS (0x8): 親の stdin/stdout/stderr から切り離す
        # CREATE_NEW_PROCESS_GROUP (0x200): 独立したプロセスグループで起動（シグナル伝播を防ぐ）
        # CREATE_BREAKAWAY_FROM_JOB (0x100): 親プロセスの Job Object から離脱してバックグラウンド生存を保証
        # CREATE_NO_WINDOW (0x08000000): ウィンドウ非表示でバックグラウンド実行
        detached_process = 0x00000008
        create_new_process_group = 0x00000200
        create_breakaway_from_job = 0x00000100
        create_no_window = 0x08000000
        kwargs["creationflags"] = (
            detached_process
            | create_new_process_group
            | create_breakaway_from_job
            | create_no_window
        )
    else:
        kwargs["start_new_session"] = True

    kwargs["env"] = env
    try:
        proc = subprocess.Popen([python_exe, str(APP)], **kwargs)  # pylint: disable=consider-using-with # nosec B603
    except OSError as exc:
        if os.name == "nt" and "creationflags" in kwargs:
            logger.debug(
                "Initial backend spawn failed with breakaway flags (%s); retrying without breakaway",
                exc,
            )
            kwargs["creationflags"] = (
                detached_process | create_new_process_group | create_no_window
            )
            proc = subprocess.Popen([python_exe, str(APP)], **kwargs)  # pylint: disable=consider-using-with # nosec B603
        else:
            raise

    tmp_pid = PID_FILE.with_suffix(".tmp")
    try:
        tmp_pid.write_text(str(proc.pid), encoding="utf-8")
        os.replace(tmp_pid, PID_FILE)
    except OSError as exc:
        logger.warning("Failed to write PID file atomically: %s", exc)
        try:
            PID_FILE.write_text(str(proc.pid), encoding="utf-8")
        except OSError:
            pass
    # The backend is launched detached; the extension already polls /api/health
    # after this call, so we must NOT block the native host's synchronous message
    # loop for up to 20s here (Chrome's native-messaging timeout is shorter). Return
    # "starting" immediately and let the caller poll. We only do a very short health
    # probe so an instantly-healthy backend still reports ok without extra round trips.
    if wait_for_backend_ready(timeout_sec=2.0):
        return {
            "ok": True,
            "message": f"Backend started (pid={proc.pid})",
            "pid": proc.pid,
            "port": port,
        }

    if is_running(proc.pid):
        return {
            "ok": True,
            "message": (
                f"Backend is still starting (pid={proc.pid});"
                " health check will be retried by the extension."
            ),
            "pid": proc.pid,
            "port": port,
            "warming_up": True,
        }

    PID_FILE.unlink(missing_ok=True)
    return {
        "ok": False,
        "error": "Backend process exited before becoming healthy.",
        "port": port,
    }


def start(extension_id=None):
    """Run startup lifecycle while holding the cross-process startup lock."""
    with _startup_lock():
        return _start(extension_id)


if __name__ == "__main__":
    print(start())
