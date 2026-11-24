@echo off
REM Daily swing-agent run. Safe to double-click; output is appended to logs\scheduled_run.log.
cd /d "%~dp0.."
".venv\Scripts\python.exe" swing.py run --log-file logs\scheduled_run.log
