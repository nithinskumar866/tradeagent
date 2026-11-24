"""Helpers that print (and on Windows optionally install) a weekday schedule for `swing.py run`."""

from __future__ import annotations

import platform
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import PROJECT_ROOT

IST = timezone(timedelta(hours=5, minutes=30))
TASK_NAME = "SwingAgentDaily"


def local_run_time(run_time_ist: str, now: datetime | None = None) -> tuple[str, str]:
    """Convert 'HH:MM' IST to this machine's local time. Returns (HH:MM local, tz description)."""
    h, m = (int(x) for x in run_time_ist.split(":"))
    now = now or datetime.now().astimezone()
    ist_dt = datetime(now.year, now.month, now.day, h, m, tzinfo=IST)
    local = ist_dt.astimezone(now.tzinfo)
    return local.strftime("%H:%M"), str(now.tzname())


def windows_command(run_time_ist: str, root: Path = PROJECT_ROOT) -> str:
    """schtasks command that runs scripts/run_daily.bat on weekdays."""
    local, _ = local_run_time(run_time_ist)
    bat = root / "scripts" / "run_daily.bat"
    return (f'schtasks /Create /TN "{TASK_NAME}" /TR "\\"{bat}\\"" /SC WEEKLY '
            f'/D MON,TUE,WED,THU,FRI /ST {local} /F')


def windows_powershell(run_time_ist: str, root: Path = PROJECT_ROOT) -> str:
    """PowerShell that registers a hidden (no console window) weekday task.

    Uses pythonw.exe so nothing can close the run by accident, and -StartWhenAvailable so a
    run missed while the laptop was off or asleep happens as soon as it's back.
    """
    local, _ = local_run_time(run_time_ist)
    pyw = root / ".venv" / "Scripts" / "pythonw.exe"
    return "; ".join([
        f"$a = New-ScheduledTaskAction -Execute '{pyw}' "
        f"-Argument 'swing.py run --log-file logs/scheduled_run.log' -WorkingDirectory '{root}'",
        f"$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At {local}",
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries "
        "-AllowStartIfOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1)",
        f"Register-ScheduledTask -TaskName '{TASK_NAME}' -Action $a -Trigger $t -Settings $s "
        "-Description 'Swing agent daily run (python swing.py run)' -Force | Out-Null",
        f"'Scheduled task {TASK_NAME} registered.'",
    ])


def cron_line(run_time_ist: str, root: Path = PROJECT_ROOT) -> str:
    """crontab line (local time) that runs scripts/run_daily.sh on weekdays."""
    local, _ = local_run_time(run_time_ist)
    h, m = local.split(":")
    return f'{int(m)} {int(h)} * * 1-5 "{root / "scripts" / "run_daily.sh"}"'


def instructions(run_time_ist: str) -> str:
    """Human-readable scheduling instructions for the current OS."""
    local, tzname = local_run_time(run_time_ist)
    system = platform.system()
    lines = [f"Scheduled time: {run_time_ist} IST = {local} on this machine ({tzname}), Monday–Friday.", ""]
    if system == "Windows":
        lines += [
            "Windows: run `python swing.py schedule --install`. It registers a task that runs hidden",
            "(no console window) and catches up if the computer was off or asleep at that time.",
            "",
            "Simpler alternative (shows a console window, no catch-up), in a Command Prompt:",
            "  " + windows_command(run_time_ist),
            "",
            f'Check it:  schtasks /Query /TN "{TASK_NAME}"',
            f'Remove it: schtasks /Delete /TN "{TASK_NAME}" /F',
            "Output goes to logs\\scheduled_run.log.",
        ]
    else:
        lines += [
            f"{'macOS' if system == 'Darwin' else 'Linux'}: run `crontab -e` and add this line:",
            "",
            "  " + cron_line(run_time_ist),
            "",
            "cron uses the machine's local time (converted above). Output goes to logs/scheduled_run.log.",
            "First make the script executable: chmod +x scripts/run_daily.sh",
        ]
    lines += ["", "You can always run it by hand: `python swing.py run`, or 'Update data now' in the dashboard."]
    return "\n".join(lines)
