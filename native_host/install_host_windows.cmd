@echo off
setlocal
PowerShell -ExecutionPolicy Bypass -File "%~dp0install_host_windows.ps1" %*
