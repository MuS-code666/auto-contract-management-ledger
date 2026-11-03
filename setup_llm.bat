@echo off
rem Contract Ledger: setup_llm
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_llm.ps1" %*
if errorlevel 1 pause
