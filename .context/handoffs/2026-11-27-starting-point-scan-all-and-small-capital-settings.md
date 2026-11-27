---
title: "Starting point: scan-all and small-capital settings"
author: nithinskumar866 (with AI assistant)
date: 2026-11-27T10:57:04
status: active
related_files:
  - src/swing_agent/market_scan.py
  - swing.py
  - config.yaml
  - data/watchlists/nifty100.csv
commit: 26205d0
last_confirmed: 2026-11-27
---

## Goal

A free, local, rule-based swing trading assistant for NSE stocks (see README). The latest work lets the scanner run on every NSE-listed stock and size trade plans for any capital, including small accounts.

## What's done

Milestones 2–7 are committed: indicators, market regime and scanner; costs, risk sizing, trade log and exit manager; portfolio backtester; Streamlit dashboard; event calendar and scheduling (hidden Windows task with catch-up); optional local Ollama explainer with faithfulness checks.
Since then: 'Analyze any stock' (rule checklist, plan, what-if capital, target-date estimate), NSE/BSE choice, any capital, showing how quantity is worked out, and `scan-all` (src/swing_agent/market_scan.py + a swing.py command + tests/test_market_scan.py), committed 2026-11-26.

## Current state

Uncommitted changes (not reviewed by the author yet):
- `config.yaml`: account.capital 100000 → 10000; universe.min_price 100 → 0 (no minimum share price)
- `data/watchlists/nifty100.csv`: NMDC added
Branch `main`, latest commit `26205d0` (scan-all).

## Files changed

- `config.yaml` (modified)
- `data/watchlists/nifty100.csv` (modified)
- `.mcp.json` (untracked)
- `AGENTS.md` (untracked)

## Decisions and why

- Rule-based on purpose: no machine learning; the same inputs always give the same output.
- The optional LLM only rewrites reasons as a paragraph and can never change a number or a decision; every reply is checked.
- Never connects to a broker or places orders: the user trades manually and logs trades.

## What was tried and didn't work

With the default settings the strategy lost money in the 2015–2026 backtest while Nifty rose (stated in the README's backtest section).

## Tests passed / not run

pytest: 150 passed on 2026-11-27 (run by Claude before writing this note). The uncommitted config change was in place during that run.

## Next step

Decide whether the uncommitted config change (capital 10,000, no minimum price) and the NMDC watchlist entry are meant to stay. Then commit or revert them, and check the scan-all output with the chosen capital.

## Risks and warnings

- min_price 0 lets very low-priced (penny) stocks into scans; check that the liquidity filter (min_avg_traded_value_cr: 20) still keeps them out.
- Don't act on signals without reading the backtest results first.
- This first note was written by Claude from git history, the README and the current diff. Correct anything that's wrong.
