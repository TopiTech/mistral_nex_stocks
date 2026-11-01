@echo off
setlocal
cd /d "%~dp0"

REM LIFECYCLE-9: keep the errorlevel capture at top level. Inside a
REM parenthesized ( ... ) block cmd.exe expands %errorlevel% when the block is
REM PARSED (before `uv run` executes), which always reported success. Top-level
REM lines are expanded per-line, after uv ran, so the real exit code propagates.
where uv >nul 2>nul
if errorlevel 1 goto uv_missing
uv run --locked python app.py
exit /b %errorlevel%

:uv_missing
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
exit /b %errorlevel%
