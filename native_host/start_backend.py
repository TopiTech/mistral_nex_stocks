#!/usr/bin/env python3
"""バックエンドプロセス起動管理モジュール"""

import logging
import os
import socket
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from typing import Any

import psutil

import requests

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
LOG = ROOT / "backend.log"
PID_FILE = ROOT / ".backend.pid"
PID_WARMUP_GRACE_SEC = 120
DEFAULT_BACKEND_PORT = 5000

logger = logging.getLogger("native_host.start_backend")
if not logger.handlers:
    file_handler = logging.FileHandler(LOG, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(file_handler)
logger.setLevel(logging.INFO)
logger.propagate = False


def get_backend_port() -> int:
    """バックエンドポート番号を環境変数から取得"""
    port_text = os.environ.get("MNS_BACKEND_PORT", "").strip()
    if port_text:
        try:
            return int(port_text)
        except ValueError:
            logger.warning(
                "Invalid MNS_BACKEND_PORT value %r; falling back to default %s",
                port_text,
                DEFAULT_BACKEND_PORT,
            )
    return DEFAULT_BACKEND_PORT


def is_port_in_use(port: int) -> bool:
    """指定ポートが使用中か確認"""
    for host in ("127.0.0.1", "localhost"):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
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
                    if 200 <= int(getattr(resp, "status_code", 0)) < 300:
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
                if 200 <= int(getattr(resp, "status_code", 0)) < 300:
                    return True
            finally:
                try:
                    resp.close()
                except Exception:
                    logger.debug("Failed to close health check response (once)")
        except requests.RequestException:
            continue
    return False


def start(extension_id=None):
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
    port = get_backend_port()
    port_in_use = is_port_in_use(port)

    if PID_FILE.exists():
        try:
            pid_text = PID_FILE.read_text(encoding="utf-8").strip()
            if pid_text:
                pid = int(pid_text)
                if is_running(pid):
                    if port_in_use or is_backend_healthy_once(timeout_sec=1.5):
                        return {
                            "ok": True,
                            "message": f"Already running (pid={pid})",
                            "pid": pid,
                            "port": port,
                        }
                    # PID が生きていてもヘルス応答が長時間得られない場合は
                    # PID再利用や別プロセス混入を疑い、古いPID情報として破棄する。
                    pid_file_age_sec = max(0.0, time.time() - PID_FILE.stat().st_mtime)
                    if pid_file_age_sec > PID_WARMUP_GRACE_SEC:
                        logger.warning(
                            "Stale backend PID detected (pid=%s age=%.1fs). Removing pid file.",
                            pid,
                            pid_file_age_sec,
                        )
                        PID_FILE.unlink(missing_ok=True)
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
            PID_FILE.unlink(missing_ok=True)
        except (OSError, ValueError):
            logger.warning("Failed to read/cleanup stale pid file: %s", PID_FILE, exc_info=True)

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
    with LOG.open("ab") as log:
        kwargs: dict[str, Any] = {
            "cwd": str(ROOT),
            "stdout": log,
            "stderr": log,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":  # pragma: no cover
            # DETACHED_PROCESS (0x8): 親の stdin/stdout/stderr から切り離す
            # CREATE_NEW_PROCESS_GROUP (0x200): 独立したプロセスグループで起動（シグナル伝播を防ぐ）
            detached_process = 0x00000008
            create_new_process_group = 0x00000200
            kwargs["creationflags"] = detached_process | create_new_process_group
        else:
            kwargs["start_new_session"] = True
        kwargs["env"] = env
        proc = subprocess.Popen([python_exe, str(APP)], **kwargs)  # pylint: disable=consider-using-with # nosec B603

    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
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


if __name__ == "__main__":
    print(start())
