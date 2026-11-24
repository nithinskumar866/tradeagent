from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from swing_agent.events import (
    EventError,
    add_event,
    check_results,
    get_events,
    import_events,
    refresh_earnings,
    results_dates_by_symbol,
    sync_events_csv,
)
from swing_agent.scheduling import cron_line, local_run_time, windows_command


# ---------------------------------------------------------------- events

def test_add_event_validates_and_dedupes(conn):
    assert add_event(conn, "2026-12-05", "all", "rbi", "policy") is not None
    assert add_event(conn, date(2026, 12, 5), "ALL", "RBI") is None          # duplicate
    with pytest.raises(EventError, match="type"):
        add_event(conn, "2026-12-05", "ALL", "PARTY")
    with pytest.raises(EventError, match="date"):
        add_event(conn, "not a date", "ALL", "RBI")
    ev = get_events(conn)
    assert ev.iloc[0][["symbol", "type", "source"]].tolist() == ["ALL", "RBI", "manual"]


def test_csv_sync_mirrors_file(conn, tmp_path):
    p = tmp_path / "events.csv"
    p.write_text("date,symbol,type,note\n2026-12-05,ALL,RBI,policy\n2027-01-15,reliance,results,\n"
                 "bad,ALL,RBI,x\n", encoding="utf-8")
    added, problems = sync_events_csv(conn, p)
    assert added == 2 and len(problems) == 1 and "row 4" in problems[0]
    assert results_dates_by_symbol(conn) == {"RELIANCE": [date(2027, 1, 15)]}
    p.write_text("date,symbol,type,note\n2026-12-05,ALL,RBI,policy\n", encoding="utf-8")  # row removed
    sync_events_csv(conn, p)
    assert results_dates_by_symbol(conn) == {}
    assert len(get_events(conn)) == 1


def test_csv_sync_creates_missing_file(conn, tmp_path):
    p = tmp_path / "sub" / "events.csv"
    assert sync_events_csv(conn, p) == (0, [])
    assert p.read_text(encoding="utf-8").startswith("date,symbol,type,note")


def test_import_requires_columns(conn):
    with pytest.raises(EventError, match="missing columns"):
        import_events(conn, pd.DataFrame({"when": ["2026-01-01"]}))


def test_refresh_earnings_best_effort_and_weekly(tmp_path):
    from swing_agent import db

    c = db.connect(tmp_path / "e.db")
    today = date(2026, 11, 24)
    calls = []

    def fake(sym):
        calls.append(sym)
        if sym == "BAD":
            raise RuntimeError("yahoo said no")
        return [date(2026, 9, 1), date(2026, 11, 10), date(2027, 1, 14)]  # first one is too old

    msg = refresh_earnings(c, ["TECHM", "BAD"], today=today, fetch=fake)
    assert "1/2" in msg and "unavailable" in msg
    assert results_dates_by_symbol(c) == {"TECHM": [date(2026, 11, 10), date(2027, 1, 14)]}
    # fresh → skipped within a week; force refetches without duplicating
    assert "fresh" in refresh_earnings(c, ["TECHM"], today=today + timedelta(days=2), fetch=fake)
    refresh_earnings(c, ["TECHM"], today=today, fetch=fake, force=True)
    assert len(results_dates_by_symbol(c)["TECHM"]) == 2
    assert check_results(results_dates_by_symbol(c)["TECHM"], date(2027, 1, 11)).trading_days_away == 3


def test_yahoo_times_convert_to_indian_dates(monkeypatch):
    """8 Jul 20:00 New York time is already 9 Jul in India."""
    import sys
    import types

    from swing_agent import events

    idx = pd.DatetimeIndex([pd.Timestamp("2026-07-08 20:00", tz="America/New_York")])

    class T:
        def __init__(self, s):
            pass

        def get_earnings_dates(self, limit):
            return pd.DataFrame({"EPS": [1.0]}, index=idx)

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=T))
    assert events._yf_earnings("TCS") == [date(2026, 7, 9)]


# ---------------------------------------------------------------- scheduling

def test_local_run_time_conversion():
    ist_now = datetime(2026, 11, 24, 9, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert local_run_time("18:30", ist_now)[0] == "18:30"
    london = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    assert local_run_time("18:30", london)[0] == "13:00"


def test_schedule_commands_point_at_scripts():
    win = windows_command("18:30")
    assert "run_daily.bat" in win and "/D MON,TUE,WED,THU,FRI" in win
    line = cron_line("18:30")
    assert line.split()[4] == "1-5" and "run_daily.sh" in line
