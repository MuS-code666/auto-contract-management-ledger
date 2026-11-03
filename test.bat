@echo off
rem Contract Ledger: run tests
cd /d "%~dp0"
".venv\Scripts\python.exe" -m pytest -q tests %*
pause
