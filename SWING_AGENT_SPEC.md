# Swing Trading Agent — Build Specification

> Hand this file to Claude Code. Put it in an empty project folder and say:
> "Read SWING_AGENT_SPEC.md and build it milestone by milestone. Stop after each milestone, show me the test results and output, and wait for my go-ahead."

---

## 1. What we are building

A **personal, free, locally-run swing trading assistant for Indian stocks (NSE)**. Every evening after market close it:

1. Downloads daily price data (the only step that needs internet).
2. Checks overall market health (Nifty trend + India VIX).
3. Scans a watchlist for swing-trade **buy setups** using fixed, rule-based logic.
4. Produces a **trade plan** for each setup: buy zone, stop-loss, target, quantity.
5. Checks the user's **open positions** and says Hold / Sell / Move stop.
6. Shows everything in a **local dashboard** and a daily text report.

It also includes a **backtester** that runs the exact same rules on 10+ years of history.

### Hard constraints
- **Free:** no paid APIs, no API keys, no subscriptions.
- **Local:** runs on the user's laptop. After the daily download, everything works offline. If the download fails, it falls back to cached data and clearly warns that data is stale.
- **Rules decide, not AI.** All buy/sell decisions come from deterministic rules. An optional local LLM (v2) may only *explain* decisions, never make or change them.
- **No order placement.** The tool never connects to a broker. The user places trades manually and logs them.
- **Personal use only.** Show a small disclaimer in the dashboard and reports: "Educational tool for personal use. Not investment advice."
- **Cross-platform:** must work on Windows, macOS and Linux. (Ask the user which OS they use for the scheduling setup.)

### Explicitly out of scope for v1
- Short selling, F&O, intraday.
- Live/tick data, news sentiment.
- Machine learning (planned for v3, see §13).
- Broker integration.

---

## 2. Tech stack

| Purpose | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | |
| Data handling | pandas, numpy | |
| Market data | yfinance | Unofficial; wrap behind an interface so the source can be swapped |
| Storage | SQLite (stdlib `sqlite3`) | Single file `data/market.db` |
| Config | YAML (`pyyaml`) + `pydantic` v2 for validation | |
| Dashboard | Streamlit + Plotly | Runs at `localhost:8501` |
| CLI | `argparse` | Single entry point `swing.py` |
| Tests | pytest | Tests must **not** need internet (use synthetic fixtures) |
| Optional AI (v2) | Ollama local HTTP API | Only if installed and running |

**Implement indicators yourself in pandas** (EMA, SMA, RSI-Wilder, ATR). Do not depend on TA-Lib or pandas-ta: fewer install problems, fully testable.

Pin dependency versions in `requirements.txt`.

---

## 3. Architecture

```
            ┌──────────────┐  ┌────────────────┐  ┌───────────────────┐
 INPUTS     │ Market data  │  │ Event calendar │  │ Settings + trades │
            └──────┬───────┘  └───────┬────────┘  └─────────┬─────────┘
                   └──────────────────┼─────────────────────┘
                                      ▼
 STORAGE                  ┌────────────────────────┐
                          │  SQLite: market.db     │
                          └───────────┬────────────┘
                                      ▼
 SIGNAL     ┌────────────┬──────────────┬───────────┬──────────────┐
 ENGINE     │ Indicators │ Market filter│  Scanner  │ Exit manager │
            └────────────┴──────────────┴───────────┴──────────────┘
                                      ▼
 DECISION   ┌──────────────┐  ┌─────────────┐  ┌──────────────────────┐
 SUPPORT    │ Risk manager │  │ Backtester  │  │ AI explainer (v2)    │
            └──────────────┘  └─────────────┘  └──────────────────────┘
                                      ▼
 OUTPUT           ┌────────────────────────────────────────┐
                  │ Streamlit dashboard + daily report .md │
                  └────────────────────────────────────────┘
```

**Single source of truth:** the live scanner, exit manager and risk manager must be the **same functions** the backtester calls. No duplicated strategy logic.

### Project layout

```
swing-agent/
├── SWING_AGENT_SPEC.md
├── README.md
├── requirements.txt
├── config.yaml
├── swing.py                    # CLI entry point
├── app.py                      # Streamlit dashboard
├── data/
│   ├── market.db
│   ├── watchlists/nifty100.csv # symbol, company name, sector
│   └── events.csv              # user-maintained event calendar
├── reports/                    # daily reports YYYY-MM-DD.md, backtest outputs
├── logs/
├── src/swing_agent/
│   ├── __init__.py
│   ├── config.py               # load + validate config.yaml
│   ├── db.py                   # schema, connections, read/write helpers
│   ├── data_sources/
│   │   ├── base.py             # DataSource interface
│   │   └── yfinance_source.py
│   ├── data_fetcher.py         # incremental updates + data-quality checks
│   ├── indicators.py
│   ├── market_filter.py        # market regime
│   ├── events.py               # event calendar + results-date lookup
│   ├── scanner.py              # entry rules + ranking
│   ├── risk.py                 # position sizing + portfolio limits
│   ├── exit_manager.py         # exit rules for open positions
│   ├── costs.py                # Indian delivery trade charges
│   ├── portfolio.py            # trade log → positions, cash, P&L
│   ├── backtest.py             # portfolio-level backtester
│   ├── metrics.py              # performance statistics
│   ├── report.py               # daily markdown/terminal report
│   ├── explainer.py            # v2: optional Ollama explanations
│   └── pipeline.py             # orchestrates the daily run
└── tests/
    ├── fixtures/               # synthetic OHLCV generators
    └── test_*.py
```

---

## 4. Configuration (`config.yaml`)

All numbers in the strategy must come from config, never hard-coded. Validate with pydantic and fail with a clear message on bad values.

```yaml
account:
  capital: 100000            # INR
  risk_per_trade_pct: 1.0    # % of capital risked per trade
  max_open_positions: 5
  max_position_pct: 20       # max % of capital in one stock
  max_per_sector: 2

universe:
  watchlist_file: data/watchlists/nifty100.csv
  min_price: 100             # INR
  min_avg_traded_value_cr: 20  # 20-day avg (close*volume) in crore INR
  min_history_bars: 250

data:
  source: yfinance
  history_years: 12
  index_symbol: "^NSEI"
  vix_symbol: "^INDIAVIX"
  refetch_overlap_days: 5

market_filter:
  ema_fast: 50
  ema_slow: 200
  vix_max: 25                 # above this → no new entries
  vix_spike_pct_5d: 30        # VIX up more than this in 5 days → no new entries
  caution_risk_multiplier: 0.5
  caution_max_positions: 3

entry:
  ema_short: 20
  ema_mid: 50
  ema_long: 200
  ema_long_slope_lookback: 20
  pullback_min_pct: 3
  pullback_max_pct: 12
  pullback_lookback: 20
  touch_tolerance_pct: 1.0    # low within 1% of EMA20 counts as a touch
  touch_lookback: 3
  rsi_period: 14
  rsi_dip_min: 35
  rsi_dip_max: 50
  rsi_dip_lookback: 5
  buy_zone_top_buffer_pct: 0.5
  avoid_results_within_days: 5   # trading days

stops_targets:
  swing_low_lookback: 5
  stop_buffer_pct: 0.5
  atr_period: 14
  min_stop_atr: 1.0           # stop at least 1 ATR below entry
  max_stop_pct: 8             # skip trade if stop is further than this
  target_r: 2.0
  breakeven_at_r: 1.0
  trend_exit_ema: 50
  time_stop_days: 20
  results_warning_days: 3

costs:                        # approximate Indian delivery charges — user must verify with their broker
  brokerage_per_order: 20
  stt_pct: 0.1                # buy and sell
  exchange_txn_pct: 0.00297
  sebi_fee_pct: 0.0001
  stamp_duty_buy_pct: 0.015
  gst_pct: 18                 # on brokerage + exchange + SEBI fees
  dp_charge_per_sell: 15.93
  slippage_pct: 0.1           # per side

backtest:
  start_date: "2014-01-01"
  in_sample_end: "2021-12-31" # report in-sample and out-of-sample separately
  initial_capital: 100000

schedule:
  run_time_ist: "18:30"

explainer:                    # v2
  enabled: false
  ollama_url: "http://localhost:11434"
  model: "llama3.1:8b"
```

---

## 5. Data layer

### 5.1 Watchlist
- Ship a static `data/watchlists/nifty100.csv` with columns `symbol,name,sector` (NSE symbols without suffix, e.g. `RELIANCE`). Populate it with the current Nifty 100 constituents.
- Add `swing.py watchlist --refresh` that tries to download the latest constituents CSV from niftyindices.com. If that fails, keep the existing file and print how to update it manually.
- The user can point `watchlist_file` to any CSV with the same columns.

### 5.2 DataSource interface
```python
class DataSource(Protocol):
    def fetch_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame: ...
    # returns columns: date, open, high, low, close, volume (date ascending)
```
- `YFinanceSource`: maps `RELIANCE` → `RELIANCE.NS`; index/VIX symbols are passed through unchanged. Use `auto_adjust=True` (split/dividend-adjusted prices).
- Batch downloads where possible, with retries and polite throttling.

### 5.3 Incremental updates
- First run: download `history_years` of data for the watchlist + index + VIX.
- Later runs: fetch from `last_stored_date - refetch_overlap_days` onward and upsert.
- **Corporate-action check:** if an overlapping stored close differs from the new close by more than 1%, the history was re-adjusted (split/bonus/dividend). Re-download that symbol's full history.

### 5.4 Data-quality checks (log warnings, show them in the report)
- Latest bar date is older than the latest Nifty bar date → symbol is stale.
- Zero or missing volume, non-positive prices, high < low, gaps over 7 calendar days.
- Symbols that fail to download are skipped for that day and listed in the report.
- **Latest trading day** = the date of the latest Nifty (`^NSEI`) bar. Don't maintain a holiday calendar.

---

## 6. Database schema (SQLite)

```sql
CREATE TABLE symbols (symbol TEXT PRIMARY KEY, name TEXT, sector TEXT, active INTEGER DEFAULT 1);
CREATE TABLE prices (
  symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER,
  PRIMARY KEY (symbol, date));
CREATE TABLE events (
  id INTEGER PRIMARY KEY, date TEXT, symbol TEXT,  -- symbol = 'ALL' for market-wide events
  type TEXT,                                        -- RESULTS | RBI | BUDGET | FED | ELECTION | OTHER
  note TEXT, source TEXT);                          -- manual | yfinance
CREATE TABLE signals (
  id INTEGER PRIMARY KEY, run_date TEXT, symbol TEXT, setup TEXT,
  signal_close REAL, buy_zone_low REAL, buy_zone_high REAL,
  stop REAL, target REAL, qty INTEGER, risk_amount REAL, rr REAL,
  score REAL, reasons_json TEXT, warnings_json TEXT, regime TEXT);
CREATE TABLE trades (
  id INTEGER PRIMARY KEY, symbol TEXT, side TEXT CHECK (side IN ('BUY','SELL')),
  date TEXT, price REAL, qty INTEGER, stop REAL, target REAL,
  fees REAL, signal_id INTEGER, note TEXT);
CREATE TABLE alerts (
  id INTEGER PRIMARY KEY, run_date TEXT, symbol TEXT,
  type TEXT,       -- STOP_HIT | TARGET_HIT | MOVE_STOP | TREND_EXIT | TIME_EXIT | RESULTS_SOON | REGIME_CHANGE | DATA
  message TEXT, suggested_action TEXT);
CREATE TABLE runs (
  run_date TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT,
  status TEXT, data_last_date TEXT, notes TEXT);
```
Positions are **derived** from `trades` (FIFO, partial sells allowed). Don't store them separately.

---

## 7. Indicators (`indicators.py`)

Pure functions that take a DataFrame and return Series. Each must use **only data up to and including the current bar** (no look-ahead).

- `ema(close, n)` = `close.ewm(span=n, adjust=False).mean()`
- `sma(x, n)`
- `rsi_wilder(close, 14)`, using Wilder's smoothing (`ewm(alpha=1/n, adjust=False)`)
- `atr_wilder(df, 14)`, using true range with Wilder's smoothing
- `avg_traded_value_cr(df, 20)` = 20-day mean of `close*volume / 1e7`
- `relative_strength(stock_close, index_close, 63)` = 63-day stock return minus 63-day Nifty return
- `add_all_indicators(df, cfg)` adds all columns at once

---

## 8. Strategy v1: "Pullback in an uptrend"

Signals are evaluated on the **close of day T**. Entries happen on **day T+1**.

### 8.1 Market regime (`market_filter.py`), evaluated on Nifty
| Regime | Condition | Effect |
|---|---|---|
| **BULLISH** | Nifty close > EMA50 **and** EMA50 > EMA200 | Normal risk and limits |
| **CAUTION** | Nifty close > EMA200 but not BULLISH | Risk × `caution_risk_multiplier`, max positions = `caution_max_positions` |
| **BEARISH** | Nifty close < EMA200 | **No new entries** |
| **FEAR** (overrides) | VIX > `vix_max` **or** VIX up > `vix_spike_pct_5d` % in 5 days | **No new entries** |

When the regime changes from the previous run, create a `REGIME_CHANGE` alert.

### 8.2 Universe filter (per stock, day T)
- close ≥ `min_price`
- 20-day avg traded value ≥ `min_avg_traded_value_cr`
- ≥ `min_history_bars` bars of history
- latest bar is not stale

### 8.3 Entry conditions (all must be true on day T)
1. **Uptrend:** close > EMA200, EMA50 > EMA200, and EMA200 today > EMA200 `ema_long_slope_lookback` bars ago.
2. **Real pullback:** `(max(high, last 20 bars) − min(low, last 5 bars)) / max(high, last 20 bars)` is between `pullback_min_pct` and `pullback_max_pct`.
3. **Touched support:** `min(low, last 3 bars) ≤ EMA20 × (1 + touch_tolerance_pct/100)`, **and** close > EMA50.
4. **RSI reset and turning:** `min(RSI, last 5 bars)` between `rsi_dip_min` and `rsi_dip_max`, and RSI today > RSI yesterday.
5. **Bullish trigger:** close > previous day's high, close > EMA20, and close > open.
6. **No results soon:** no `RESULTS` event for this symbol within `avoid_results_within_days` trading days. (If no date is known, don't block. Add the warning "results date unknown".)
7. **Regime allows entries** (BULLISH or CAUTION).

### 8.4 Bonus score (for ranking, not required)
- +1 if average volume over the 4 days before T < 20-day average volume (volume dried up on the pullback).
- +1 if volume on day T > 20-day average volume (buyers stepping in).
- Base score = relative strength vs Nifty (63-day). **Final score = RS + 2 × bonus points.**

### 8.5 Trade plan for each signal
- **Buy zone:** from day-T close to `day-T high × (1 + buy_zone_top_buffer_pct/100)`. Valid for day T+1 only. If T+1 opens above the zone, skip it (gap-up; don't chase).
- **Stop-loss:** `min(low, last swing_low_lookback bars) × (1 − stop_buffer_pct/100)`. If `entry − stop < min_stop_atr × ATR`, widen it to `entry − min_stop_atr × ATR`. If `(entry − stop)/entry > max_stop_pct`, **reject** the signal ("stop too wide").
- **R** = entry − stop. **Target** = entry + `target_r` × R.
- For the plan shown to the user, use the top of the buy zone as the planning entry (conservative).
- **Reasons:** store human-readable reason strings in `reasons_json`, e.g. "Uptrend: price above 200 EMA (₹2,410)", "Pulled back 6.2% from 20-day high", "RSI dipped to 41 and is turning up", "Closed above yesterday's high". These strings are also the non-AI explanation.

### 8.6 Ranking and selection
- Sort signals by score, descending.
- Free slots = regime max positions − currently open positions.
- Walk down the list and skip any signal that breaks `max_per_sector` or that you already hold. Mark the top signals up to the number of free slots as **TAKE**, and the rest as **WATCH** (still shown).

---

## 9. Risk manager (`risk.py`)

```
risk_amount = capital × risk_per_trade_pct/100 × (regime multiplier)
qty = floor(risk_amount / (entry − stop))
qty = min(qty, floor(capital × max_position_pct/100 / entry))
qty = min(qty, floor(available_cash / entry))
if qty < 1 → reject ("position too small for this capital")
```
- **Capital** for live mode = config capital + realised P&L (net of costs).
- **Available cash** = capital − cost of open positions.
- `costs.py` computes round-trip charges using the `costs` config (brokerage, STT on both sides, exchange charges, SEBI fee, stamp duty on buy, GST, DP charge on sell) plus slippage. Show the estimated costs in each trade plan.

---

## 10. Exit manager (`exit_manager.py`)

Run this every day for each open position, using the user's **actual** entry price, stop and target from the trade log. Check in this priority order on the latest bar:

| # | Rule | Alert type | Suggested action |
|---|---|---|---|
| 1 | low ≤ stop | `STOP_HIT` | "Stop-loss ₹X was hit (low ₹Y). If not already out, sell at tomorrow's open." |
| 2 | high ≥ target | `TARGET_HIT` | "Target ₹X reached. Book profit." |
| 3 | close ≥ entry + `breakeven_at_r` × R and stop < entry | `MOVE_STOP` | "Move stop-loss to breakeven ₹entry." (Update the stored stop once the user confirms in the dashboard.) |
| 4 | close < EMA(`trend_exit_ema`) | `TREND_EXIT` | "Trend broken: closed below 50 EMA. Sell at tomorrow's open." |
| 5 | held ≥ `time_stop_days` trading days | `TIME_EXIT` | "Trade hasn't worked in 20 days. Consider exiting." |
| 6 | `RESULTS` event within `results_warning_days` | `RESULTS_SOON` | "Results on DATE. Consider exiting or reducing size." |
| — | none of the above | — | **HOLD** (show current P&L in ₹ and in R) |

Regime is BEARISH or FEAR → add a note to every position: "Market weak. Consider tightening stops."

The README and dashboard should recommend placing a real stop-loss/GTT order with the broker, since this tool only checks once a day.

---

## 11. Backtester (`backtest.py`)

**Portfolio-level, day-by-day simulation that reuses the live scanner, risk and exit functions.**

### 11.1 Rules
- Iterate trading days in order. On each day T, compute signals using data **up to T only**.
- **Fills on T+1:**
  - Skip if T+1 open > buy-zone top, or if T+1 open ≤ stop.
  - Otherwise fill at `T+1 open × (1 + slippage)`. Recompute stop distance, R and target from the actual fill.
- **Exits:**
  - If low ≤ stop, exit at `min(open, stop)` (gap-down fills at the open).
  - If high ≥ target, exit at `max(open, target)`.
  - If both happen on the same bar, **assume the stop hit first** (conservative).
  - Trend exits and time exits fill at the next day's open.
  - Breakeven stop moves apply from the next bar.
- Apply all costs and slippage on every fill.
- Enforce the same position limits, sector limits, cash limits and regime rules as live mode.
- Ignore results-date filters in the backtest, because historical NSE results dates aren't reliably available for free. Say so in the report.

### 11.2 Metrics (`metrics.py`)
Total return, CAGR, max drawdown (% and duration), number of trades, win rate, average win and average loss (₹ and R), profit factor, expectancy (R per trade), longest losing streak, average holding days, % time invested, yearly returns table, and benchmark comparison against Nifty buy-and-hold over the same period.

Report everything for **in-sample** (start → `in_sample_end`), **out-of-sample** (after `in_sample_end`) and **full period**.

### 11.3 Outputs
- `reports/backtest_<timestamp>/trades.csv`, `equity.csv` and `summary.md`
- Charts in the dashboard: equity curve vs Nifty, drawdown, and R-multiple distribution.

### 11.4 Required honesty notes, printed in every backtest summary
- **Survivorship bias:** the current Nifty 100 list is applied to past years, which overstates results.
- Fills are modelled on daily bars. Real fills, slippage and gaps differ.
- Past performance doesn't guarantee future results.
- Don't keep tuning parameters until the backtest looks good (overfitting). Judge the strategy on its out-of-sample results.

### 11.5 Optional parameter sensitivity
`swing.py backtest --sweep target_r=1.5,2,2.5 rsi_dip_max=45,50,55` runs a small grid and prints a table. The point is to show whether results are stable across nearby values, not to pick the best one.

---

## 12. Daily pipeline, CLI and dashboard

### 12.1 CLI (`swing.py`)
```
python swing.py init                 # create DB, load watchlist, first full download
python swing.py update               # incremental data update
python swing.py scan                 # regime + signals + exits, print report
python swing.py run                  # update + scan + save report (used by scheduler)
python swing.py backtest [--start --end --sweep ...]
python swing.py trade buy  SYMBOL --price P --qty Q [--stop S --target T --date D]
python swing.py trade sell SYMBOL --price P --qty Q [--date D]
python swing.py events add --date D --symbol SYM|ALL --type RESULTS --note "..."
python swing.py watchlist --refresh
python swing.py dashboard            # launches streamlit
```
`run` must be **idempotent**: re-running it on the same day replaces that day's signals and alerts without creating duplicates. Record each run in the `runs` table.

### 12.2 Daily report (`reports/YYYY-MM-DD.md` + terminal output)
1. Header: data date, regime (with Nifty vs EMAs and the VIX value), any data warnings.
2. **Exit alerts** for open positions (most urgent first).
3. **Buy signals** marked TAKE: symbol, sector, buy zone, stop, target, qty, ₹ risk, R:R, estimated costs, reasons, warnings.
4. Signals marked WATCH.
5. Upcoming events in the next 7 days.
6. Disclaimer.

### 12.3 Streamlit dashboard (`app.py`)
Pages:
1. **Today:** regime banner (green/amber/red), exit alerts, TAKE and WATCH tables. Each signal has a "Log this buy" button that pre-fills the trade form.
2. **My positions:** open positions with live-from-DB price, P&L (₹, %, R), status, stop and target, days held, and a "Log sell" button.
3. **Chart:** pick a symbol and see a candlestick chart with EMA 20/50/200, a volume panel and an RSI panel, plus entry, stop and target lines if a signal or position exists.
4. **Backtest:** run a backtest or load a past one. Show metrics, equity vs Nifty, drawdown, trades table and yearly table.
5. **Journal:** closed trades with P&L and R, win rate, and expectancy of the user's *actual* trading.
6. **Events:** view, add and delete calendar entries. Import a CSV.
7. **Settings:** read-only view of `config.yaml` with its validation status.

The dashboard reads from the DB only. It never downloads data itself, except through an explicit "Update data now" button.

### 12.4 Event calendar (`events.py`)
- Main source: `data/events.csv`, maintained by the user, with columns `date,symbol,type,note`, synced into the DB.
- Best-effort: try `yfinance` earnings dates for each symbol and store them with `source=yfinance`. Never fail the run if this breaks.
- The README should tell the user to add RBI policy dates, the Union Budget, Fed meetings and similar events manually each year.

### 12.5 Scheduling (README instructions + helper)
- Windows: provide a `schtasks` command (or a `.bat` file) that runs `python swing.py run` on weekdays at `run_time_ist`.
- macOS/Linux: provide a crontab line for weekdays, noting the machine's timezone.
- If the laptop was off at the scheduled time, `run` can simply be triggered manually or from the dashboard button.

---

## 13. Future versions (do **not** build now; just leave clean extension points)

- **v2: AI explainer** (`explainer.py`). If `explainer.enabled` is true and Ollama responds at `ollama_url`, send it the signal's computed facts (numbers + reason strings) and ask for a 3–4 sentence plain-English explanation. The prompt must forbid changing numbers or giving a different recommendation. If Ollama isn't available, fall back silently to the rule-based reason strings. Never block the pipeline on the LLM.
- **v3: ML ranker.** A model (e.g. gradient boosting) that scores rule-based signals using features at signal time. It's trained on backtest trades with a strict time-based train/test split. It may only re-rank signals, never create them. Keep it in mind when structuring `scanner.py` (scores should be pluggable).
- **v3: News sentiment.** RSS headlines + local LLM classification, used only as a warning flag.
- **Alternative data source:** an NSE bhavcopy `DataSource` implementation.

---

## 14. Testing requirements

All tests run offline using synthetic data generated in `tests/fixtures/`.

- **Indicators:** EMA matches `pandas.ewm(adjust=False)`. RSI and ATR match hand-computed values on a small known series. RSI stays within [0, 100].
- **No look-ahead:** for random T, the signals computed from `df[:T]` equal the signals computed from the full df but evaluated at T.
- **Scanner:** a synthetic "perfect pullback" series triggers a signal. A downtrend, a too-deep pullback or a broken EMA50 does not.
- **Market filter:** synthetic Nifty series produce each regime. A VIX spike produces FEAR.
- **Risk:** the sizing math, all caps (position %, cash, qty < 1) and the caution multiplier.
- **Costs:** the round-trip cost on a known trade matches a hand calculation.
- **Exits:** stop hit, target hit, gap-down through stop (fills at open), gap-up through target, stop and target on the same bar (stop wins), breakeven move, trend exit, time exit.
- **Portfolio:** partial sells, FIFO P&L, cash and capital updates.
- **Backtest integration:** a full run on 3 years of synthetic data for 10 symbols completes. Trades respect max positions and sector limits. The equity curve has no NaNs.
- **Pipeline:** running `run` twice on the same day doesn't duplicate rows.

---

## 15. Milestones (build in this order; stop after each one for user review)

| # | Milestone | Done when |
|---|---|---|
| 1 | Skeleton, config + validation, DB schema, watchlist, data fetcher with incremental updates and quality checks | `python swing.py init` downloads ~12 years for the watchlist + Nifty + VIX. `update` adds only new bars. Tests pass. |
| 2 | Indicators, market regime, scanner, reason strings | `python swing.py scan` prints the regime and today's signals with plans. Indicator, no-look-ahead and scanner tests pass. |
| 3 | Costs, risk manager, trade log/portfolio, exit manager, daily report | The user can log trades from the CLI. `scan` shows exit alerts for positions. `run` writes `reports/YYYY-MM-DD.md`. |
| 4 | Backtester + metrics + sweep | `python swing.py backtest` produces summary/trades/equity files with in-sample and out-of-sample sections and the honesty notes. All exit-fill tests pass. |
| 5 | Streamlit dashboard (all pages) | `python swing.py dashboard` opens it, and all pages work against real data. |
| 6 | Event calendar, scheduling helpers, README | The README covers install, first run, daily routine, adding events, scheduling on Windows/macOS/Linux, config explanation and disclaimers. |
| 7 | *(Optional, only when the user asks)* Ollama explainer | Works when Ollama is running and falls back cleanly when it isn't. |

### General coding standards
- Type hints everywhere, with docstrings on public functions.
- Use the `logging` module and write to `logs/swing.log`. No bare `print` except in the CLI and report output.
- Show friendly error messages for common problems: no internet, empty watchlist, invalid config, DB locked.
- Keep each module focused, with no circular imports.
- Don't add dependencies beyond §2 without asking the user.
