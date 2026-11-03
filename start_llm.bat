@echo off
rem Contract Ledger: start_llm
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_llm.ps1" %*
if errorlevel 1 pause
