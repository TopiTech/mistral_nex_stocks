@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if not errorlevel 1 (
    uv run --locked python app.py
    exit /b %errorlevel%
)

REM Fallback: no uv found — use direct python (may ignore lockfile)
echo [run_app.bat] uv not found; falling back to system python (lockfile not enforced)
set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
if not defined PYTHON_EXE (
	where py >nul 2>nul
	if not errorlevel 1 set "PYTHON_EXE=py -3"
)
if not defined PYTHON_EXE set "PYTHON_EXE=python"

%PYTHON_EXE% app.py
