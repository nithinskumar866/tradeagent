#!/usr/bin/env sh
# Daily swing-agent run (used by cron on macOS/Linux).
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
echo "===== $(date) =====" >> logs/scheduled_run.log
.venv/bin/python swing.py run >> logs/scheduled_run.log 2>&1
