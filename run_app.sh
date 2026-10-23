#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
    exec uv run --locked python app.py
fi

allow_unlocked="$(echo "${MNS_ALLOW_UNLOCKED:-}" | tr '[:upper:]' '[:lower:]')"
if [ "$allow_unlocked" = "1" ] || [ "$allow_unlocked" = "true" ] || [ "$allow_unlocked" = "yes" ]; then
    echo "[run_app.sh] uv not found; falling back to system python (lockfile NOT enforced, MNS_ALLOW_UNLOCKED=1)"
    PYTHON_BIN=""
    if [ -x ".venv/bin/python" ]; then
        PYTHON_BIN=".venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON_BIN="python3"
    elif command -v python >/dev/null 2>&1; then
        PYTHON_BIN="python"
    else
        echo "[run_app.sh] No python interpreter found." >&2
        exit 1
    fi
    exec "$PYTHON_BIN" app.py
fi

echo "[run_app.sh] uv not found. Refusing to start without lockfile." >&2
echo "Install uv or set MNS_ALLOW_UNLOCKED=1 to allow unlocked fallback (not recommended)." >&2
exit 1
