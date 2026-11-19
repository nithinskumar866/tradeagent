"""Daily report text (terminal + markdown)."""

from __future__ import annotations

from datetime import date

from . import DISCLAIMER
from .config import Config
from .fmt import inr, pct
from .scanner import CONDITION_LABELS, CONDITIONS, ScanResult, Signal

REGIME_ICON = {"BULLISH": "🟢", "CAUTION": "🟠", "BEARISH": "🔴", "FEAR": "🔴"}


def _signal_block(s: Signal) -> list[str]:
    lines = [
        f"### {s.symbol} — {s.name} ({s.sector})  [{s.status}]"
        + (f"  _{s.status_note}_" if s.status_note else ""),
        f"- Buy zone: {inr(s.buy_zone_low)} – {inr(s.buy_zone_high)} (valid next trading day only; "
        f"skip if it opens above {inr(s.buy_zone_high)})",
        f"- Stop-loss: {inr(s.stop)} ({pct(s.stop_pct)} below planned entry)   "
        f"Target: {inr(s.target)}   R:R {s.rr:.1f}   Score {s.score:.1f}",
    ]
    if s.qty is not None:
        lines.append(f"- Qty: {s.qty}   Risk: {inr(s.risk_amount)}   Est. round-trip costs: {inr(s.est_costs)}")
    lines.append("- Why:")
    lines += [f"  - {r}" for r in s.reasons]
    if s.warnings:
        lines.append("- Warnings: " + "; ".join(s.warnings))
    return lines


def format_scan(result: ScanResult, cfg: Config, prev_regime: str | None = None,
                today: date | None = None) -> str:
    """Markdown report for a scan result (regime, TAKE, WATCH, funnel, disclaimer)."""
    r = result.regime
    out = [f"# Swing scan — data as of {result.asof}", ""]
    if today and (today - result.asof).days > 3:
        out += [f"> ⚠️ Latest data is from {result.asof}, {(today - result.asof).days} days ago. "
                "Run `python swing.py update`.", ""]
    out.append(f"## Market: {REGIME_ICON.get(r.regime, '')} {r.regime}")
    out.append(r.description)
    if prev_regime and prev_regime != r.regime:
        out.append(f"**Regime changed from {prev_regime} to {r.regime}.**")
    out.append("")

    if result.stale:
        out += [f"Data warnings: no bar on {result.asof} for {', '.join(sorted(result.stale))} (skipped).", ""]

    take = [s for s in result.signals if s.status == "TAKE"]
    watch = [s for s in result.signals if s.status == "WATCH"]
    out.append(f"## Buy signals — TAKE ({len(take)})")
    if not take:
        out.append("None today." if r.allows_entries else f"No new entries while the market is {r.regime}.")
    for s in take:
        out += [""] + _signal_block(s)
    out += ["", f"## WATCH ({len(watch)})"]
    if not watch:
        out.append("None.")
    for s in watch:
        out += [""] + _signal_block(s)

    out += ["", f"## Scan funnel ({result.scanned} symbols)"]
    for k in CONDITIONS:
        if result.funnel.get(k):
            out.append(f"- {result.funnel[k]}: {CONDITION_LABELS[k]}")
    out.append(f"- {result.funnel.get('passed', 0)}: passed all technical conditions")
    if result.rejected:
        out.append("- Rejected after passing:")
        out += [f"  - {sym}: {why}" for sym, why in result.rejected]
    out += ["", "---", f"_{DISCLAIMER}_"]
    return "\n".join(out)
