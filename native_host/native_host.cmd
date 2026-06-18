@echo off
setlocal
cd /d "%~dp0"
for %%i in (python.exe python3.exe py.exe) do (
    where %%i >nul 2>&1 && (
        "%%i" -u "%~dp0native_host.py" %*
        goto :eof
    )
)
echo [ERROR] Python not found in PATH. Please install Python or add it to PATH. >&2
exit /b 1
