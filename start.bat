@echo off
rem Contract Ledger: start app (http://127.0.0.1:8765/)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1" %*
pause
