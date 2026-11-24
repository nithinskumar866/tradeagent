@echo off
REM Daily swing-agent run (used by Windows Task Scheduler). Safe to double-click too.
cd /d "%~dp0.."
if not exist logs mkdir logs
echo ===== %date% %time% ===== >> "logs\scheduled_run.log"
".venv\Scripts\python.exe" swing.py run >> "logs\scheduled_run.log" 2>&1
