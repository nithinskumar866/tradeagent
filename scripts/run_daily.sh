#!/usr/bin/env sh
# Daily swing-agent run (used by cron on macOS/Linux). Output is appended to logs/scheduled_run.log.
cd "$(dirname "$0")/.." || exit 1
.venv/bin/python swing.py run --log-file logs/scheduled_run.log
