"""Swing Trading Agent: Streamlit dashboard.

Reads from the local database only. It downloads data solely through the explicit
"Update data now" button. Educational tool for personal use. Not investment advice.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from swing_agent import DISCLAIMER, db  # noqa: E402
from swing_agent.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, ConfigError, load_config  # noqa: E402
from swing_agent.costs import round_trip_costs  # noqa: E402
from swing_agent.events import EVENT_TYPES, EventError, add_event, delete_event, get_events, import_events  # noqa: E402
from swing_agent.exit_manager import EXIT_NOW, MOVE_STOP, check_position  # noqa: E402
from swing_agent.fmt import inr  # noqa: E402
from swing_agent.market_filter import compute_regime_series, regime_at  # noqa: E402
from swing_agent.portfolio import PortfolioError, build_portfolio  # noqa: E402
from swing_agent.trading import TradeError, log_buy, log_sell, move_stop  # noqa: E402

# ---------------------------------------------------------------- palette (dataviz reference instance)
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]           # categorical slots 1–3 (validated all-pairs)
GOOD, CRITICAL, WARNING = "#0ca30c", "#d03b3b", "#fab219"
MUTED, GRID = "#8a8985", "rgba(128,128,128,0.18)"
REGIME_STYLE = {
    "BULLISH": ("success", "🟢"), "CAUTION": ("warning", "🟠"),
    "BEARISH": ("error", "🔴"), "FEAR": ("error", "🔴"),
}
REPORTS = Path(os.environ.get("SWING_AGENT_REPORTS") or PROJECT_ROOT / "reports")
PAGES = ["Today", "My positions", "Chart", "Backtest", "Journal", "Events", "Settings"]

st.set_page_config(page_title="Swing Agent", page_icon="📈", layout="wide")


# ---------------------------------------------------------------- data access

def conn():
    """A fresh connection per call (SQLite connections shouldn't cross Streamlit threads)."""
    return db.connect()


def db_stamp() -> float:
    """Changes whenever the DB file changes, used as a cache key."""
    path = Path(os.environ.get("SWING_AGENT_DB") or db.DEFAULT_DB_PATH)
    stamps = [p.stat().st_mtime for p in (path, Path(str(path) + "-wal")) if p.exists()]
    return max(stamps) if stamps else 0.0


@st.cache_data(show_spinner=False)
def cached_frames(symbols: tuple[str, ...], stamp: float, cfg_json: str):
    """Indicator/setup frames for some symbols + regime series (cached per DB change)."""
    from swing_agent.pipeline import load_market_data

    cfg = load_config()
    md = load_market_data(conn(), cfg, symbols=list(symbols))
    return md.frames, md.regime_series, md.meta


def get_cfg():
    try:
        return load_config(), None
    except ConfigError as err:
        return None, str(err)


def chart_layout(fig: go.Figure, height: int) -> go.Figure:
    fig.update_layout(height=height, margin=dict(l=10, r=10, t=30, b=10), hovermode="x unified",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
                      plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    fig.update_xaxes(showgrid=False, linecolor=GRID)
    fig.update_yaxes(gridcolor=GRID, zeroline=False)
    return fig


def money_cols(df: pd.DataFrame, cols: list[str], decimals: int = 2) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        out[c] = out[c].map(lambda x: inr(x, decimals) if pd.notna(x) else "–")
    return out


# ---------------------------------------------------------------- sidebar

cfg, cfg_err = get_cfg()
with st.sidebar:
    st.title("📈 Swing Agent")
    page = st.radio("Page", PAGES, key="page", label_visibility="collapsed")
    st.divider()
    if cfg is not None:
        if st.button("Update data now", help="Download new prices, then scan and save today's report",
                     use_container_width=True):
            from swing_agent.pipeline import daily

            with st.spinner("Downloading prices and scanning…"):
                try:
                    res = daily(conn(), cfg, do_update=True, persist=True)
                    msg = f"Done. Data as of {res.run_date}."
                    if res.update and res.update.offline:
                        st.warning("Every download failed (no internet?). Used cached data.")
                    st.cache_data.clear()
                    st.session_state["flash"] = msg
                    st.rerun()
                except Exception as err:  # show, don't crash the app
                    st.error(f"Run failed: {err}")
        if st.button("Re-scan (no download)", use_container_width=True):
            from swing_agent.pipeline import daily

            with st.spinner("Scanning…"):
                try:
                    res = daily(conn(), cfg, do_update=False, persist=True)
                    st.cache_data.clear()
                    st.session_state["flash"] = f"Scan saved for {res.run_date}."
                    st.rerun()
                except Exception as err:
                    st.error(f"Scan failed: {err}")
    st.divider()
    st.caption(DISCLAIMER)

if "flash" in st.session_state:
    st.toast(st.session_state.pop("flash"))

if cfg is None:
    st.error(cfg_err)
    st.stop()


# ---------------------------------------------------------------- shared pieces

def latest_run() -> dict | None:
    row = conn().execute("SELECT * FROM runs WHERE status='OK' ORDER BY run_date DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def regime_banner(run_date: str | None) -> None:
    c = conn()
    idx = db.get_prices(c, cfg.data.index_symbol)
    if idx.empty:
        st.info("No market data yet. Run `python swing.py init` in a terminal (first download takes about a minute).")
        st.stop()
    series = compute_regime_series(idx, db.get_prices(c, cfg.data.vix_symbol), cfg)
    state = regime_at(series, run_date or idx.index[-1], cfg)
    kind, icon = REGIME_STYLE.get(state.regime, ("info", ""))
    getattr(st, kind)(f"{icon} **Market: {state.regime}** (data {state.date}). {state.description}")
    latest = idx.index[-1].date()
    if run_date and date.fromisoformat(run_date) < latest:
        st.warning(f"Prices go up to {latest} but the last saved scan is for {run_date}. "
                   "Click **Re-scan** in the sidebar.")


def buy_form(prefill: dict | None, key: str) -> None:
    p = prefill or {}
    sfx = f"{key}_{p.get('signal_id', 'manual')}"
    with st.form(f"buy_{sfx}", clear_on_submit=False):
        st.markdown(f"**Log a buy**" + (f" (from signal #{p['signal_id']})" if p.get("signal_id") else ""))
        c1, c2, c3 = st.columns(3)
        sym = c1.text_input("Symbol", value=p.get("symbol", ""), key=f"bsym_{sfx}")
        price = c2.number_input("Fill price", min_value=0.0, value=float(p.get("price", 0.0)), step=0.05,
                                key=f"bpx_{sfx}")
        qty = c3.number_input("Quantity", min_value=0, value=int(p.get("qty", 0) or 0), step=1, key=f"bq_{sfx}")
        c4, c5, c6 = st.columns(3)
        stop = c4.number_input("Stop-loss", min_value=0.0, value=float(p.get("stop", 0.0)), step=0.05,
                               key=f"bst_{sfx}")
        target = c5.number_input("Target (0 = 2R from fill)", min_value=0.0, value=0.0, step=0.05,
                                 key=f"btg_{sfx}")
        when = c6.date_input("Date", value=date.today(), key=f"bd_{sfx}")
        fees = st.number_input("Actual charges (0 = estimate)", min_value=0.0, value=0.0, step=1.0, key=f"bf_{sfx}")
        if st.form_submit_button("Save buy"):
            try:
                res = log_buy(conn(), cfg, sym, price, int(qty), when, stop=stop or None,
                              target=target or None, fees=fees or None)
                st.success(f"Logged BUY #{res.trade_id}. " + " ".join(res.messages))
                st.cache_data.clear()
            except (TradeError, PortfolioError) as err:
                st.error(str(err))


# ---------------------------------------------------------------- pages

def page_today() -> None:
    st.header("Today")
    run = latest_run()
    regime_banner(run["run_date"] if run else None)
    if not run:
        st.info("No saved scan yet. Click **Update data now** (or run `python swing.py run`).")
        return
    rd = run["run_date"]
    c = conn()

    alerts = pd.read_sql_query("SELECT symbol, type, message, suggested_action FROM alerts WHERE run_date=?",
                               c, params=[rd])
    st.subheader(f"Exit alerts ({len(alerts)})")
    if alerts.empty:
        st.caption("No alerts.")
    for a in alerts.itertuples():
        text = f"**{a.type}** · {a.message} → {a.suggested_action}"
        (st.error if a.type in EXIT_NOW else st.warning if a.type != "DATA" else st.info)(text)

    sig = pd.read_sql_query(
        "SELECT s.*, y.name, y.sector FROM signals s LEFT JOIN symbols y ON y.symbol = s.symbol "
        "WHERE s.run_date = ? ORDER BY s.score DESC", c, params=[rd])
    for status in ("TAKE", "WATCH"):
        part = sig[sig["status"] == status]
        st.subheader(f"{status} ({len(part)})")
        if part.empty:
            st.caption("None.")
            continue
        view = part[["symbol", "sector", "buy_zone_low", "buy_zone_high", "stop", "target", "qty",
                     "risk_amount", "rr", "score", "status_note"]].copy()
        view["est_costs"] = [round_trip_costs(r.buy_zone_high, r.target, int(r.qty or 0), cfg.costs)
                             if r.qty else None for r in part.itertuples()]
        view = money_cols(view, ["buy_zone_low", "buy_zone_high", "stop", "target", "risk_amount", "est_costs"])
        view["score"] = part["score"].round(1)
        view["rr"] = part["rr"].round(1)
        view = view.drop(columns=["sector"]).rename(columns={
            "symbol": "Symbol", "buy_zone_low": "Zone from", "buy_zone_high": "Zone to", "stop": "Stop",
            "target": "Target", "qty": "Qty", "risk_amount": "Risk", "rr": "R:R", "score": "Score",
            "est_costs": "Costs", "status_note": "Note"})
        st.dataframe(view, hide_index=True, use_container_width=True)
        for r in part.itertuples():
            with st.expander(f"{r.symbol} · {r.name or ''} · why and warnings"):
                if isinstance(r.explanation, str) and r.explanation:
                    st.info(r.explanation)
                    st.caption("Explained by your local AI model from the facts below. The rules decided; "
                               "the AI only explains.")
                for reason in json.loads(r.reasons_json or "[]"):
                    st.markdown(f"- {reason}")
                warns = json.loads(r.warnings_json or "[]")
                if warns:
                    st.caption("Warnings: " + "; ".join(warns))
                if st.button("Log this buy", key=f"logbuy_{r.id}"):
                    st.session_state["prefill"] = {"symbol": r.symbol, "price": r.buy_zone_high,
                                                   "qty": r.qty, "stop": r.stop, "signal_id": r.id}
    st.divider()
    buy_form(st.session_state.get("prefill"), "today")
    st.caption("Tip: also place a real stop-loss / GTT order with your broker. "
               "This tool only checks once a day after the close.")


def page_positions() -> None:
    st.header("My positions")
    c = conn()
    try:
        pf = build_portfolio(db.get_trades(c), cfg.account.capital)
    except PortfolioError as err:
        st.error(f"The trade log is inconsistent: {err}")
        return
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Capital", inr(pf.capital, 0))
    m2.metric("Invested", inr(pf.invested, 0))
    m3.metric("Cash", inr(pf.available_cash, 0))
    m4.metric("Realised P&L", inr(pf.realised_pnl, 0))
    if not pf.positions:
        st.info("No open positions. Log a buy on the Today page.")
    else:
        frames, regime, _ = cached_frames(tuple(sorted(pf.positions)), db_stamp(), cfg.model_dump_json())
        asof = regime.index[-1].date()
        state = regime_at(regime, asof, cfg)
        rows, statuses = [], []
        for sym, pos in sorted(pf.positions.items()):
            s = check_position(pos, frames.get(sym), asof, cfg, None, state)
            statuses.append((s, pos))
            rows.append({"symbol": sym, "qty": s.qty, "entry": s.entry, "last": s.last_close, "P&L": s.pnl,
                         "P&L %": None if s.pnl_pct is None else round(s.pnl_pct, 2),
                         "R": None if s.r_now is None else round(s.r_now, 2), "stop": s.stop, "target": s.target,
                         "days held": s.days_held, "status": s.action})
        df = money_cols(pd.DataFrame(rows), ["entry", "last", "P&L", "stop", "target"])
        st.dataframe(df, hide_index=True, use_container_width=True)
        for s, pos in statuses:
            if s.alert:
                (st.error if s.action in EXIT_NOW else st.warning)(
                    f"**{s.action}** · {s.alert.message} → {s.alert.suggested_action}")
            if s.action == MOVE_STOP and st.button(f"Confirm: move {s.symbol} stop to breakeven "
                                                    f"{inr(pos.avg_entry)}", key=f"be_{s.symbol}"):
                st.success(" ".join(move_stop(c, cfg, s.symbol, round(pos.avg_entry, 2))))
                st.cache_data.clear()
            for n in s.notes:
                st.caption(f"{s.symbol}: {n}")

        st.subheader("Log a sell")
        with st.form("sell"):
            c1, c2, c3, c4 = st.columns(4)
            sym = c1.selectbox("Symbol", sorted(pf.positions))
            last = statuses[[p.symbol for _, p in statuses].index(sym)][0].last_close or 0.0
            price = c2.number_input("Price", min_value=0.0, value=float(last), step=0.05)
            qty = c3.number_input("Quantity", min_value=1, value=int(pf.positions[sym].qty), step=1)
            when = c4.date_input("Date", value=date.today())
            fees = st.number_input("Actual charges (0 = estimate)", min_value=0.0, value=0.0)
            if st.form_submit_button("Save sell"):
                try:
                    res = log_sell(c, cfg, sym, price, int(qty), when, fees=fees or None)
                    st.success(f"Logged SELL #{res.trade_id}. " + " ".join(res.messages))
                    st.cache_data.clear()
                except (TradeError, PortfolioError) as err:
                    st.error(str(err))
    st.caption("Place a real stop-loss / GTT order with your broker; this tool checks once a day.")

    with st.expander("Trade log (fix mistakes)"):
        trades = db.get_trades(c)
        if trades.empty:
            st.caption("No trades logged yet.")
        else:
            st.dataframe(trades.drop(columns=["stop_initial"], errors="ignore"), hide_index=True,
                         use_container_width=True)
            tid = st.selectbox("Delete trade id", trades["id"].tolist(), key="del_trade")
            if st.button("Delete this trade", type="secondary"):
                db.delete_trade(c, int(tid))
                st.cache_data.clear()
                st.rerun()
    with st.expander("Log a buy manually"):
        buy_form(None, "positions")


def page_chart() -> None:
    st.header("Chart")
    c = conn()
    syms = db.get_symbols(c)["symbol"].tolist()
    pf = build_portfolio(db.get_trades(c), cfg.account.capital)
    syms = sorted(set(syms) | set(pf.positions))
    if not syms:
        st.info("No symbols yet.")
        return
    default = st.session_state.get("chart_symbol")
    if default is None:  # first TAKE signal of the latest run, else first held position
        row = c.execute("SELECT symbol FROM signals WHERE run_date = (SELECT MAX(run_date) FROM signals) "
                        "ORDER BY status = 'TAKE' DESC, score DESC LIMIT 1").fetchone()
        default = row[0] if row else next(iter(sorted(pf.positions)), None)
    col1, col2 = st.columns([2, 1])
    sym = col1.selectbox("Symbol", syms, index=syms.index(default) if default in syms else 0)
    span = col2.radio("Range", ["6M", "1Y", "2Y", "5Y"], index=1, horizontal=True)
    frames, _, _ = cached_frames((sym,), db_stamp(), cfg.model_dump_json())
    f = frames.get(sym)
    if f is None or f.empty:
        st.warning("No price data for this symbol.")
        return
    months = {"6M": 6, "1Y": 12, "2Y": 24, "5Y": 60}[span]
    f = f[f.index >= f.index[-1] - pd.DateOffset(months=months)]

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, row_heights=[0.62, 0.16, 0.22],
                        vertical_spacing=0.03)
    fig.add_trace(go.Candlestick(x=f.index, open=f["open"], high=f["high"], low=f["low"], close=f["close"],
                                 name="Price", increasing_line_color=GOOD, decreasing_line_color=CRITICAL,
                                 increasing_fillcolor=GOOD, decreasing_fillcolor=CRITICAL), row=1, col=1)
    e = cfg.entry
    for col, n, color in (("ema_s", e.ema_short, SERIES[0]), ("ema_m", e.ema_mid, SERIES[1]),
                          ("ema_l", e.ema_long, SERIES[2])):
        fig.add_trace(go.Scatter(x=f.index, y=f[col], name=f"EMA {n}", line=dict(color=color, width=2)),
                      row=1, col=1)
    up = f["close"] >= f["open"]
    fig.add_trace(go.Bar(x=f.index, y=f["volume"], name="Volume", showlegend=False,
                         marker_color=[GOOD if u else CRITICAL for u in up], opacity=0.5), row=2, col=1)
    fig.add_trace(go.Scatter(x=f.index, y=f["rsi"], name="RSI", showlegend=False,
                             line=dict(color=SERIES[0], width=2)), row=3, col=1)
    for lvl in (30, 70):
        fig.add_hline(y=lvl, line=dict(color=MUTED, width=1, dash="dot"), row=3, col=1)
    fig.add_hrect(y0=e.rsi_dip_min, y1=e.rsi_dip_max, fillcolor=SERIES[0], opacity=0.07, line_width=0,
                  row=3, col=1)

    levels = []
    pos = pf.positions.get(sym)
    if pos:
        levels = [("Entry", pos.avg_entry, MUTED), ("Stop", pos.stop, CRITICAL), ("Target", pos.target, GOOD)]
    else:
        s = db.latest_signal(c, sym)
        if s is not None:
            levels = [(f"Buy zone top ({s['run_date']})", s["buy_zone_high"], MUTED), ("Stop", s["stop"], CRITICAL),
                      ("Target", s["target"], GOOD)]
    for label, val, color in levels:
        if val:
            fig.add_hline(y=val, line=dict(color=color, width=1.5, dash="dash"), row=1, col=1,
                          annotation_text=f"{label} {inr(val)}", annotation_position="top left",
                          annotation_font_color=color)
    fig.update_layout(xaxis_rangeslider_visible=False)
    fig.update_yaxes(title_text="₹", row=1, col=1)
    fig.update_yaxes(title_text="Vol", row=2, col=1)
    fig.update_yaxes(title_text="RSI", range=[0, 100], row=3, col=1)
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])
    st.plotly_chart(chart_layout(fig, 720), use_container_width=True)
    last = f.iloc[-1]
    st.caption(f"{sym} · last close {inr(last['close'])} on {f.index[-1].date()} · RSI {last['rsi']:.0f} · "
               f"ATR {inr(last['atr'])} · shaded band = RSI dip zone {e.rsi_dip_min:g}–{e.rsi_dip_max:g}")


def _load_backtest(folder: Path):
    from swing_agent.metrics import period_metrics, split_periods

    eq = pd.read_csv(folder / "equity.csv", parse_dates=["date"]).set_index("date")
    tr = pd.read_csv(folder / "trades.csv")
    periods = {n: period_metrics(e, t) for n, (e, t) in split_periods(eq, tr, cfg.backtest.in_sample_end).items()}
    return eq, tr, periods


def page_backtest() -> None:
    from swing_agent.backtest import HONESTY_NOTES, METRIC_LABELS, backtest_from_cache, load_prices, write_outputs
    from swing_agent.metrics import drawdown, fmt_metric, yearly_returns

    st.header("Backtest")
    with st.expander("Run a new backtest", expanded=False):
        c1, c2 = st.columns(2)
        start = c1.date_input("Start", value=cfg.backtest.start_date)
        end = c2.date_input("End", value=date.today())
        if st.button("Run backtest"):
            with st.spinner("Simulating… (about 10 seconds)"):
                try:
                    res = backtest_from_cache(load_prices(conn(), cfg), cfg, start, end)
                    out = REPORTS / f"backtest_{datetime.now():%Y%m%d_%H%M%S}"
                    write_outputs(res, cfg, out)
                    st.session_state["bt_folder"] = out.name
                    st.success(f"Saved to {out}")
                except ValueError as err:
                    st.error(str(err))
    folders = sorted([p for p in REPORTS.glob("backtest_*") if (p / "equity.csv").exists()], reverse=True)
    if not folders:
        st.info("No backtests yet. Run one above or with `python swing.py backtest`.")
        return
    names = [p.name for p in folders]
    chosen = st.session_state.get("bt_folder")
    pick = st.selectbox("Backtest", names, index=names.index(chosen) if chosen in names else 0)
    eq, tr, periods = _load_backtest(REPORTS / pick)

    table = pd.DataFrame({n: [fmt_metric(k, periods[n].get(k)) for k, _ in METRIC_LABELS] for n in periods},
                         index=[label for _, label in METRIC_LABELS])
    st.dataframe(table, use_container_width=True, height=(len(table) + 1) * 35 + 3)

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=eq.index, y=eq["equity"], name="Strategy", line=dict(color=SERIES[0], width=2)))
    fig.add_trace(go.Scatter(x=eq.index, y=eq["benchmark"], name="Nifty buy & hold",
                             line=dict(color=SERIES[1], width=2)))
    fig.add_vline(x=pd.Timestamp(cfg.backtest.in_sample_end), line=dict(color=MUTED, width=1, dash="dot"),
                  annotation_text="out-of-sample →", annotation_position="top right")
    fig.update_yaxes(title_text="Equity (₹)")
    st.subheader("Equity vs Nifty")
    st.plotly_chart(chart_layout(fig, 380), use_container_width=True)

    c1, c2 = st.columns(2)
    dd = drawdown(eq["equity"])
    fdd = go.Figure(go.Scatter(x=dd.index, y=dd, name="Drawdown", fill="tozeroy",
                               line=dict(color=SERIES[0], width=1.5)))
    fdd.update_yaxes(title_text="Drawdown %")
    c1.subheader("Drawdown")
    c1.plotly_chart(chart_layout(fdd, 300), use_container_width=True)
    if len(tr):
        fr = go.Figure(go.Histogram(x=tr["r_multiple"], xbins=dict(size=0.25), name="Trades",
                                    marker_color=SERIES[0]))
        fr.add_vline(x=0, line=dict(color=MUTED, width=1))
        fr.update_layout(bargap=0.08)
        fr.update_xaxes(title_text="R multiple per trade")
        fr.update_yaxes(title_text="Trades")
        c2.subheader("R-multiple distribution")
        c2.plotly_chart(chart_layout(fr, 300), use_container_width=True)

    st.subheader("Yearly returns")
    yr = yearly_returns(eq).round(1)
    st.dataframe(yr.rename(columns={"strategy_pct": "strategy %", "nifty_pct": "Nifty %",
                                    "trades_open_days_pct": "days with positions %"}),
                 hide_index=True, use_container_width=True)
    st.subheader(f"Trades ({len(tr)})")
    st.dataframe(tr.round(2), hide_index=True, use_container_width=True)
    st.subheader("Read this before trusting the numbers")
    for n in HONESTY_NOTES + ["Results-date filters are ignored in backtests (no free history of results dates)."]:
        st.markdown(f"- {n}")


def page_journal() -> None:
    from swing_agent.metrics import trade_stats

    st.header("Journal (your real trades)")
    try:
        pf = build_portfolio(db.get_trades(conn()), cfg.account.capital)
    except PortfolioError as err:
        st.error(str(err))
        return
    if not pf.closed:
        st.info("No closed trades yet. Sold positions show up here with their P&L and R.")
        return
    df = pd.DataFrame([{
        "symbol": t.symbol, "entry_date": t.entry_date, "exit_date": t.exit_date, "qty": t.qty,
        "entry": t.entry_price, "exit": t.exit_price, "charges": t.fees, "pnl": t.pnl_net,
        "r_multiple": t.r_multiple, "bars_held": t.holding_days} for t in pf.closed])
    s = trade_stats(df)
    m = st.columns(5)
    m[0].metric("Closed trades", s["trades"])
    m[1].metric("Win rate", f"{s['win_rate_pct']:.0f}%")
    m[2].metric("Expectancy", "–" if pd.isna(s["expectancy_r"]) else f"{s['expectancy_r']:+.2f} R")
    m[3].metric("Net P&L", inr(df["pnl"].sum(), 0))
    m[4].metric("Profit factor", "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}")
    view = money_cols(df.rename(columns={"bars_held": "days held"}), ["entry", "exit", "charges", "pnl"])
    view["r_multiple"] = df["r_multiple"].round(2)
    st.dataframe(view, hide_index=True, use_container_width=True)


def page_events() -> None:
    st.header("Events")
    c = conn()
    st.caption("Add results dates for stocks, and RBI policy, Union Budget, Fed meetings etc. with symbol ALL. "
               "The scanner skips stocks with results within "
               f"{cfg.entry.avoid_results_within_days} trading days.")
    ev = get_events(c, start=date.today() - pd.Timedelta(days=30))
    st.dataframe(ev, hide_index=True, use_container_width=True)
    with st.form("add_event", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
        d = c1.date_input("Date", value=date.today())
        sym = c2.text_input("Symbol (or ALL)", value="ALL")
        t = c3.selectbox("Type", EVENT_TYPES)
        note = c4.text_input("Note")
        if st.form_submit_button("Add event"):
            try:
                new = add_event(c, d, sym, t, note)
                st.success("Added." if new else "Already in the calendar.")
                st.rerun()
            except EventError as err:
                st.error(str(err))
    c1, c2 = st.columns(2)
    if not ev.empty:
        eid = c1.selectbox("Delete event id", ev["id"].tolist())
        if c1.button("Delete event"):
            delete_event(c, int(eid))
            st.rerun()
    up = c2.file_uploader("Import CSV (date,symbol,type,note)", type="csv")
    if up is not None and c2.button("Import"):
        try:
            added, problems = import_events(c, pd.read_csv(up, dtype=str))
            c2.success(f"Imported {added} event(s).")
            for p in problems:
                c2.warning(p)
        except EventError as err:
            c2.error(str(err))


def page_settings() -> None:
    st.header("Settings")
    st.caption("Read-only. Edit config.yaml in a text editor, then reload this page.")
    try:
        load_config()
        st.success("config.yaml is valid.")
    except ConfigError as err:
        st.error(str(err))
    st.code(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"), language="yaml")
    s = db.price_summary(conn())
    st.caption(f"Database: {db.DEFAULT_DB_PATH} · {len(s)} symbols · {int(s['bars'].sum()) if len(s) else 0} bars")


{"Today": page_today, "My positions": page_positions, "Chart": page_chart, "Backtest": page_backtest,
 "Journal": page_journal, "Events": page_events, "Settings": page_settings}[page]()
