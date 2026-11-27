---
title: Small-capital defaults committed; penny-stock filter checked
author: nithinskumar866 (with AI assistant)
date: 2026-11-27T18:03:33
status: active
related_files:
  - config.yaml
  - src/swing_agent/scanner.py
  - src/swing_agent/indicators.py
commit: 96f45e8
last_confirmed: 2026-11-27
---

## Goal

Let the scanner run on every NSE stock and size plans for small accounts.

## What's done

Checked that the liquidity filter still works with min_price 0, then committed config.yaml (capital 10000, min_price 0) and the NMDC watchlist entry as 96f45e8.

## Current state

Branch main at 96f45e8. Working tree clean.

## Files changed

No uncommitted changes.

## Decisions and why

Kept min_price 0. The universe filter (scanner.py c_universe) requires all three checks together: close >= min_price, 20-day avg close*volume >= 20 cr, and >= 250 bars. On the nse_all.db data for 2026-11-26 (2421 symbols): 0 of 145 stocks under Rs 10 pass, 10 of 120 between Rs 10 and 20 pass, 17 of 214 between 20 and 50 pass, 29 of 258 between 50 and 100 pass. The ones that pass are heavily traded (IDEA, YESBANK, PCJEWELLER, SUZLON) and still need an uptrend and a pullback setup.

## What was tried and didn't work

_(not filled in)_

## Tests passed / not run

pytest (via .venv): 150 passed before the commit. System python has no pytest; use .venv/Scripts/python.

## Next step

Run scan-all with capital 10000 and check the plans and quantities it gives.

## Risks and warnings

Speculative stocks priced Rs 10–20 can pass (JYOTISTRUC, MOTISONS, JPPOWER, RPOWER, HCC). The 20-day average traded value is a mean, so a single volume spike can lift a stock over 20 cr. If that becomes a problem, set min_price to about 20 or use a median.
