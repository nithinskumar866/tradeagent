"""Daily report (markdown, also printed to the terminal)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import DISCLAIMER
from .config import Config
from .exit_manager import EXIT_NOW
from .fmt import inr, pct
from .scanner import CONDITION_LABELS, CONDITIONS, Signal

if TYPE_CHECKING:
    from .pipeline import DailyResult

REGIME_ICON = {"BULLISH": "🟢", "CAUTION": "🟠", "BEARISH": "🔴", "FEAR": "🔴"}
BROKER_STOP_TIP = ("Tip: place a real stop-loss / GTT order with your broker. "
                   "This tool only checks once a day after the close.")


def signal_block(s: Signal) -> list[str]:
    """Markdown lines describing one signal and its trade plan."""
    lines = [
        f"### {s.symbol} — {s.name} ({s.sector})  [{s.status}]"
        + (f"  _{s.status_note}_" if s.status_note else ""),
        f"- Buy zone: {inr(s.buy_zone_low)} – {inr(s.buy_zone_high)} (next trading day only; "
        f"skip if it opens above {inr(s.buy_zone_high)})",
        f"- Stop-loss: {inr(s.stop)} ({pct(s.stop_pct)} below planned entry)   "
        f"Target: {inr(s.target)}   R:R {s.rr:.1f}   Score {s.score:.1f}",
    ]
    if s.target_eta:
        lines.append(f"- Target date (estimate): {s.target_eta}")
    if s.qty:
        lines.append(f"- Qty: {s.qty}   Position: {inr(s.qty * s.entry_plan, 0)}   "
                     f"Risk: {inr(s.risk_amount)}   Est. round-trip costs: {inr(s.est_costs)}")
    if s.explanation:
        lines.append(f"- In plain English (local AI, rules unchanged): {s.explanation}")
    lines.append("- Why:")
    lines += [f"  - {r}" for r in s.reasons]
    if s.warnings:
        lines.append("- Warnings: " + "; ".join(s.warnings))
    if s.signal_id:
        lines.append(f"- Log it: `python swing.py trade buy {s.symbol} --price <fill> --qty {s.qty or '<qty>'}` "
                     f"(stop/target are taken from signal #{s.signal_id})")
    return lines


def format_daily(d: "DailyResult", cfg: Config) -> str:
    """The full daily report: header, exit alerts, positions, TAKE, WATCH, events, disclaimer."""
    res, r, pf = d.scan, d.scan.regime, d.portfolio
    out = [f"# Swing report — {d.run_date}", ""]
    out.append(f"## Market: {REGIME_ICON.get(r.regime, '')} {r.regime}")
    out.append(r.description)
    if d.prev_regime and d.prev_regime != r.regime:
        out.append(f"**Regime changed from {d.prev_regime} to {r.regime}.**")
    out.append("")
    out.append(f"Capital {inr(pf.capital, 0)} (realised P&L {inr(pf.realised_pnl, 0)})  ·  "
               f"Invested {inr(pf.invested, 0)}  ·  Cash {inr(pf.available_cash, 0)}  ·  "
               f"Open positions {len(pf.positions)}/{r.max_positions or cfg.account.max_open_positions}")
    if d.data_warnings:
        out += ["", "**Data warnings**"] + [f"- {w}" for w in d.data_warnings[:30]]
        if len(d.data_warnings) > 30:
            out.append(f"- … and {len(d.data_warnings) - 30} more (see logs/swing.log)")

    out += ["", f"## Exit alerts ({len(d.alerts)})"]
    if not d.alerts:
        out.append("None.")
    for a in d.alerts:
        flag = "🔴 " if a.type in EXIT_NOW else ""
        out.append(f"- {flag}**{a.type}** {a.message} → {a.suggested_action}")

    out += ["", f"## My positions ({len(d.positions)})"]
    if not d.positions:
        out.append("No open positions.")
    else:
        out.append("| Symbol | Qty | Entry | Last | P&L | R | Stop | Target | Days | Status |")
        out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---|")
        for p in d.positions:
            r_txt = f"{p.r_now:+.2f}" if p.r_now is not None else "–"
            pnl_txt = f"{inr(p.pnl, 0)} ({pct(p.pnl_pct, signed=True)})" if p.pnl is not None else "–"
            out.append(f"| {p.symbol} | {p.qty} | {inr(p.entry)} | {inr(p.last_close)} | {pnl_txt} | {r_txt} | "
                       f"{inr(p.stop)} | {inr(p.target)} | {p.days_held} | {p.action} |")
        notes = sorted({n for p in d.positions for n in p.notes})
        out += [f"- {n}" for n in notes]
        out.append(f"- {BROKER_STOP_TIP}")

    take = [s for s in res.signals if s.status == "TAKE"]
    watch = [s for s in res.signals if s.status == "WATCH"]
    out += ["", f"## Buy signals — TAKE ({len(take)})"]
    if not take:
        out.append("None today." if r.allows_entries else f"No new entries while the market is {r.regime}.")
    for s in take:
        out += [""] + signal_block(s)
    out += ["", f"## WATCH ({len(watch)})"]
    if not watch:
        out.append("None.")
    for s in watch:
        out += [""] + signal_block(s)

    out += ["", "## Upcoming events (next 7 days)"]
    if d.events.empty:
        out.append("None recorded. Add RESULTS, RBI, Budget and Fed dates with `swing.py events add`.")
    else:
        out += [f"- {e.date} {e.symbol} {e.type}: {e.note or ''}" for e in d.events.itertuples()]

    out += ["", f"## Scan funnel ({res.scanned} symbols)"]
    for k in CONDITIONS:
        if res.funnel.get(k):
            out.append(f"- {res.funnel[k]}: {CONDITION_LABELS[k]}")
    out.append(f"- {res.funnel.get('passed', 0)}: passed all technical conditions")
    if res.rejected:
        out.append("- Rejected after passing:")
        out += [f"  - {sym}: {why}" for sym, why in res.rejected]
    out += ["", "---", f"_{DISCLAIMER}_"]
    return "\n".join(out)
