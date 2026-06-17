@echo off
setlocal
cd /d "%~dp0"
"C:\Users\mibu0\AppData\Local\Programs\Python\Python314\python.exe" -u "%~dp0native_host.py" %*
