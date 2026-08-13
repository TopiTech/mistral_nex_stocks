@echo off
setlocal
cd /d "%~dp0"

where uv >nul 2>nul
if not errorlevel 1 (
    uv run --locked python app.py
    exit /b %errorlevel%
)

if "%MNS_ALLOW_UNLOCKED%"=="1" goto fallback
if /I "%MNS_ALLOW_UNLOCKED%"=="true" goto fallback
if /I "%MNS_ALLOW_UNLOCKED%"=="yes" goto fallback
echo [run_app.bat] uv not found. Refusing to start without lockfile.>&2
echo Install uv or set MNS_ALLOW_UNLOCKED=1 to allow unlocked fallback (not recommended).>&2
exit /b 1

:fallback
REM Opt-in unlocked fallback (pinning not enforced).
 echo [run_app.bat] uv not found; falling back to system python (lockfile NOT enforced, MNS_ALLOW_UNLOCKED=1)
set "PYTHON_EXE="
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
if not defined PYTHON_EXE (
	where py >nul 2>nul
	if not errorlevel 1 set "PYTHON_EXE=py -3"
)
if not defined PYTHON_EXE set "PYTHON_EXE=python"

%PYTHON_EXE% app.py
