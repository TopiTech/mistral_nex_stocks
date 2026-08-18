#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if command -v uv >/dev/null 2>&1; then
    exec uv run --locked python app.py
fi

echo "Project virtual environment not found. Run 'uv sync --locked' first." >&2
exit 1
