# Swing Trading Agent (NSE)

A free, local, rule-based swing trading assistant for Indian stocks. Every evening it downloads daily prices, checks the market's health, scans the Nifty 100 for "pullback in an uptrend" setups, writes a trade plan (buy zone, stop-loss, target, quantity) for each one, and tells you what to do with the positions you already hold. A backtester runs the same rules over 10+ years of history.

> **Educational tool for personal use. Not investment advice.**
> The rules decide; nothing here is a recommendation. The tool never connects to a broker or places orders. You place trades yourself and log them here.

**Read the backtest section before trading any signal.** With the default settings, the strategy *lost money* in the 2015–2026 backtest while Nifty rose.

---

## Contents

1. [Install](#1-install)
2. [First run](#2-first-run)
3. [Daily routine](#3-daily-routine)
4. [Logging your trades](#4-logging-your-trades)
5. [Event calendar](#5-event-calendar)
6. [Scheduling the daily run](#6-scheduling-the-daily-run)
7. [Dashboard](#7-dashboard)
8. [Backtesting](#8-backtesting)
9. [How the strategy works](#9-how-the-strategy-works)
10. [Configuration (`config.yaml`)](#10-configuration-configyaml)
11. [Troubleshooting](#11-troubleshooting)
12. [Project layout and tests](#12-project-layout-and-tests)

---

## 1. Install

You need **Python 3.11 or newer** and an internet connection for the daily download. Nothing else: no API keys, no paid data.

**Windows (PowerShell or Command Prompt)**

```bat
cd path\to\tradeagent
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**macOS / Linux**

```sh
cd path/to/tradeagent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

In the commands below, `python` means the virtual-environment Python: `.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on macOS/Linux. Activating the venv (`.venv\Scripts\activate` or `source .venv/bin/activate`) lets you type plain `python`.

## 2. First run

```sh
python swing.py init
```

This creates `data/market.db`, loads the watchlist (`data/watchlists/nifty100.csv`) and downloads about 12 years of daily prices for the 100 stocks plus Nifty 50 and India VIX. It takes about a minute.

Optional, but worth doing once: get the current official Nifty 100 list.

```sh
python swing.py watchlist --refresh
python swing.py update
```

If the download from niftyindices.com fails, the existing list is kept and the command prints how to update it by hand.

Then look at today's scan:

```sh
python swing.py scan
```

## 3. Daily routine

After the market closes (data is final after about 4 pm IST; the default schedule is 6:30 pm):

```sh
python swing.py run
```

`run` downloads the new bars, refreshes results dates (weekly), syncs your event calendar, scans, sizes the signals, checks your open positions and saves:

- the signals and alerts in the database (re-running the same day replaces them; no duplicates)
- a report in `reports/YYYY-MM-DD.md`

The report contains, in order: the market regime, data warnings, **exit alerts** for your positions (most urgent first), your positions, **TAKE** signals (up to your free position slots), **WATCH** signals, upcoming events and the scan funnel.

Then, in the evening:

1. Act on exit alerts: stop hit, target hit, trend broken, time stop, results soon, move stop to breakeven.
2. For each TAKE signal you want: next morning, buy only if the stock opens **inside the buy zone**. If it gaps above the zone, skip it, don't chase.
3. **Place a real stop-loss or GTT order with your broker.** This tool only checks once a day after the close.
4. Log what you actually did (next section).

`python swing.py scan` shows the same report without downloading or saving anything. Use `scan --date 2024-03-15` to see what the rules said on a past day.

If the download fails (no internet), `run` uses the cached data and warns that it may be stale.

## 4. Logging your trades

The tool never sees your broker account, so log every buy and sell:

```sh
python swing.py trade buy TECHM --price 1815 --qty 3
python swing.py trade sell TECHM --price 1900 --qty 3
python swing.py trade list
```

- **Buy:** if you don't give `--stop`, the stop comes from the latest signal for that stock. The target defaults to 2R from *your* fill price. Charges are estimated unless you pass `--fees` with the real amount from your contract note. `--date 2026-11-24` logs a past trade.
- **Sell:** partial sells are fine; they are matched first-in-first-out.
- **Move stop:** `python swing.py trade move-stop TECHM --stop 1815` (for example after a MOVE_STOP alert). Your R stays measured from the original stop.
- **Fix a mistake:** `trade list` shows ids; `python swing.py trade delete 7` removes one.

Positions, cash, capital and P&L are always rebuilt from this log. Capital = `account.capital` + realised P&L after charges.

## 5. Event calendar

Results dates matter: the scanner skips a stock with results within 5 trading days, and the exit check warns 3 days before results on a stock you hold.

**Results dates are fetched automatically** from Yahoo Finance once a week (about 1–2 minutes during `run`). They are a best-effort estimate; check the date on NSE before relying on it. Fetch now with:

```sh
python swing.py events fetch
```

**Market-wide events you should add yourself each year**: RBI monetary policy dates, the Union Budget, US Fed (FOMC) meetings, election results and anything else that moves the market. Put them in **`data/events.csv`**, which is synced on every `run`:

```csv
date,symbol,type,note
2027-02-01,ALL,BUDGET,Union Budget
2027-01-14,TECHM,RESULTS,Q3 results (confirmed on NSE)
```

- `symbol`: an NSE symbol, or `ALL` for market-wide events.
- `type`: one of `RESULTS`, `RBI`, `BUDGET`, `FED`, `ELECTION`, `OTHER`.
- Editing or deleting a row in the CSV changes the calendar on the next `run` (or `python swing.py events sync`).

Quick one-off additions: `python swing.py events add --date 2027-02-06 --symbol ALL --type RBI --note "policy"`. List with `python swing.py events list`. The dashboard's Events page can add, delete and import CSVs too.

## 6. Scheduling the daily run

```sh
python swing.py schedule
```

prints the exact command for your operating system, with the IST run time (`schedule.run_time_ist`, default 18:30) converted to your computer's time zone.

**Windows.** `scripts\run_daily.bat` runs `swing.py run` and appends the output to `logs\scheduled_run.log`. Either let the tool create the Task Scheduler entry:

```bat
python swing.py schedule --install
```

or run the printed command yourself, which looks like:

```bat
schtasks /Create /TN "SwingAgentDaily" /TR "\"C:\path\to\tradeagent\scripts\run_daily.bat\"" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 18:30 /F
```

Check it with `schtasks /Query /TN "SwingAgentDaily"`; remove it with `schtasks /Delete /TN "SwingAgentDaily" /F`.

**macOS / Linux.** Make the script executable and add a cron line (`crontab -e`). Cron uses the machine's local time, so use the time `swing.py schedule` prints. On an IST machine:

```sh
chmod +x scripts/run_daily.sh
# crontab line: weekdays at 18:30
30 18 * * 1-5 "/path/to/tradeagent/scripts/run_daily.sh"
```

On macOS, cron may need Full Disk Access (System Settings → Privacy & Security) to reach your project folder.

**If the computer was off or asleep** at that time, nothing runs. Just run `python swing.py run`, or click **Update data now** in the dashboard.

## 7. Dashboard

```sh
python swing.py dashboard
```

Opens http://localhost:8501 in your browser (Ctrl+C in the terminal to stop it).

| Page | What it shows |
|---|---|
| **Today** | Market regime banner (green / amber / red), exit alerts, TAKE and WATCH tables. Expand a signal for its reasons and warnings; **Log this buy** fills in the buy form. |
| **My positions** | Capital, cash, P&L in ₹, % and R, status, stop, target, days held. Log a sell, confirm a breakeven stop move, view or fix the trade log. |
| **Chart** | Candles with EMA 20/50/200, volume and RSI panels, and entry/stop/target lines when a signal or position exists. |
| **Backtest** | Run a new backtest or open a past one: metrics, equity vs Nifty, drawdown, R-multiple distribution, yearly table, trades. |
| **Journal** | Your real closed trades with win rate, expectancy (R) and net P&L. |
| **Events** | View, add, delete and import calendar entries. |
| **Settings** | Your `config.yaml` and whether it is valid. |

The dashboard only reads the local database. It downloads data only when you click **Update data now** (or **Re-scan**, which scans without downloading).

## 8. Backtesting

```sh
python swing.py backtest
python swing.py backtest --start 2018-01-01 --end 2023-12-31
```

The backtester replays every trading day using the **same functions** as the live scan, sizing and exit checks. Entries fill at the next day's open (skipped if it gaps above the buy zone or opens below the stop); stops fill at the stop or the gap-down open; targets at the target or the gap-up open; if a stop and target are hit on the same day it assumes the stop; trend and time exits fill at the next open. Charges and 0.1% slippage apply to every fill. Results-date filters are not applied (no free history of NSE results dates).

Output goes to `reports/backtest_<timestamp>/`: `summary.md`, `trades.csv`, `equity.csv`. Results are reported for **in-sample** (to `backtest.in_sample_end`, default end of 2021), **out-of-sample** (after it) and the full period, against Nifty buy-and-hold.

**What the default strategy did (2015-11-27 to 2026-11-19, ₹1,00,000 start):**

| | Strategy | Nifty buy & hold |
|---|---:|---:|
| CAGR, full period | −2.9% | +11.2% |
| CAGR, out-of-sample (2022–2026) | −6.4% | +8.2% |
| Max drawdown | −36.8% | −38.4% |
| Trades / win rate / expectancy | 820 / 33% / −0.05R | |

Before charges the rules had a thin edge (about +0.10R per trade); charges on small positions (about 0.15R per trade at ₹1 lakh capital with ₹20 brokerage) turned it negative. Trend exits (close below the 50 EMA) were the biggest losers. A small sweep of nearby settings lost money in every combination. Treat the daily signals as a starting point for your own judgement, not as a proven system.

**Parameter sensitivity:**

```sh
python swing.py backtest --sweep target_r=1.5,2,2.5 rsi_dip_max=45,50,55
```

runs a small grid and saves a table to `reports/sweep_<timestamp>.csv`. Use it to see whether results are *stable* across nearby values, not to pick the best one. Keys can be written as `section.key` (e.g. `costs.brokerage_per_order=20,0`).

Every summary repeats these warnings:

- **Survivorship bias:** today's Nifty 100 list is applied to past years. Stocks that dropped out of the index are missing, which overstates results.
- Fills are modelled on daily bars; real fills, slippage and gaps differ.
- Past performance doesn't guarantee future results.
- Don't keep tuning parameters until the backtest looks good. Judge any change on its out-of-sample results.

## 9. How the strategy works

Signals are computed on the close of day T; entries happen on day T+1.

**Market regime** (Nifty 50 and India VIX):

| Regime | Condition | Effect |
|---|---|---|
| 🟢 BULLISH | Nifty > 50 EMA and 50 EMA > 200 EMA | normal risk |
| 🟠 CAUTION | Nifty > 200 EMA but not BULLISH | half risk, max 3 positions |
| 🔴 BEARISH | Nifty < 200 EMA | no new entries |
| 🔴 FEAR | VIX > 25, or VIX up > 30% in 5 days | no new entries |

**Entry rules** (all must hold on day T): price ≥ ₹100, 20-day average traded value ≥ ₹20 crore, 250+ days of history; uptrend (close > 200 EMA, 50 EMA > 200 EMA, 200 EMA rising); a 3–12% pullback from the 20-day high; a recent low touching the 20 EMA while closing above the 50 EMA; RSI dipped to 35–50 and turning up; a bullish trigger (close above yesterday's high and the 20 EMA, and above the open); no results within 5 trading days; regime allows entries.

**Ranking:** score = relative strength vs Nifty over 63 days + 2 × bonus points (volume dried up on the pullback; volume above average on the trigger day). The top signals fill your free position slots (TAKE), respecting max 2 per sector; the rest are WATCH.

**Trade plan:** buy zone = day-T close to day-T high + 0.5% (next day only). Stop = lowest low of 5 days − 0.5%, at least 1 ATR below entry; signals needing a stop wider than 8% are rejected. Target = entry + 2R. Quantity risks 1% of capital (0.5% in CAUTION), capped at 20% of capital per stock and by available cash. Each plan shows estimated round-trip charges.

**Exit checks** for open positions, in priority order: stop hit → target hit → move stop to breakeven (once up 1R) → close below 50 EMA → 20 trading days without working → results within 3 days → hold. In BEARISH or FEAR markets every position also gets "Market weak. Consider tightening stops."

Every signal comes with plain-English reasons, e.g. "Pulled back 6.2% from 20-day high" and "RSI dipped to 41 and is turning up".

## 10. Configuration (`config.yaml`)

Every number the strategy uses lives in `config.yaml`. It is checked when any command starts; a bad value stops with a message naming the field (e.g. `entry.pullback_min_pct must be smaller than pullback_max_pct`).

| Section | What to change there |
|---|---|
| `account` | **`capital`** (your trading capital in ₹), `risk_per_trade_pct`, `max_open_positions`, `max_position_pct`, `max_per_sector` |
| `universe` | `watchlist_file` (any CSV with `symbol,name,sector`), minimum price, liquidity and history |
| `data` | years of history, index and VIX symbols, `events_file`, `fetch_results_dates` |
| `market_filter` | regime EMAs, VIX limits, CAUTION risk multiplier and position cap |
| `entry` | the entry rules above (EMAs, pullback range, RSI dip range, buy-zone buffer, results gap) |
| `stops_targets` | stop buffer, ATR floor, max stop %, target R, breakeven trigger, trend-exit EMA, time stop, results warning |
| `costs` | **check these against your broker**: brokerage per order, STT, exchange, SEBI, stamp duty, GST, DP charge, slippage |
| `backtest` | start date, in-sample end, starting capital |
| `schedule` | `run_time_ist` used by `swing.py schedule` |
| `explainer` | reserved for the optional local-LLM explainer (not built) |

Set `costs.brokerage_per_order: 0` if your broker charges nothing for delivery trades; it noticeably changes the backtest.

## 11. Troubleshooting

| Problem | What to do |
|---|---|
| `No Nifty data in the database` | Run `python swing.py init`. |
| "Every download failed" / stale-data warning | Check your internet connection and run `python swing.py run` again. Cached data is used meanwhile. |
| A stock is "older than Nifty's" latest bar | Yahoo sometimes publishes a stock's bar late. It is skipped that day and catches up on the next update. |
| A symbol fails to download | It may have been renamed or removed (e.g. LTIM became LTM). Run `python swing.py watchlist --refresh`. |
| `The database is locked` | Another run or the dashboard is writing. Wait a moment and retry. |
| `Invalid config.yaml` | The message names the field and the problem. |
| Need detail | Everything is logged to `logs/swing.log`; scheduled runs also write `logs/scheduled_run.log`. |

To experiment without touching your real data, copy `data/market.db` and set `SWING_AGENT_DB` to the copy's path before running commands.

## 12. Project layout and tests

```
swing.py            command-line entry point
app.py              Streamlit dashboard
config.yaml         all settings
data/               market.db, watchlists/nifty100.csv, events.csv
reports/            daily reports, backtests, sweeps
logs/               swing.log, scheduled_run.log
scripts/            run_daily.bat (Windows), run_daily.sh (macOS/Linux)
src/swing_agent/    the code: data_fetcher, indicators, market_filter, scanner, risk,
                    exit_manager, costs, portfolio, backtest, metrics, events, report, pipeline
tests/              offline tests on synthetic data
```

Run the tests (no internet needed):

```sh
python -m pytest
```

---

*Educational tool for personal use. Not investment advice. Prices come from Yahoo Finance via the unofficial `yfinance` library and may be delayed, adjusted or wrong; verify before trading.*
