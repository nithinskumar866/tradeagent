"""v2: optional plain-English explanations of signals from a local Ollama model.

The rules decide; the model only explains. The prompt gives it the signal's computed facts
(numbers + rule-based reason strings) and forbids changing numbers or recommending anything
different. Every reply is checked: any number not found in the facts, or wording that
contradicts the signal, rejects the explanation. On any problem (Ollama not running, model
missing, timeout, bad reply) we fall back silently to the rule-based reasons. Never blocks
the pipeline for long: one availability check with a short timeout and an overall time budget.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Callable

from .config import Config
from .fmt import inr
from .market_filter import RegimeState
from .scanner import Signal

log = logging.getLogger(__name__)

# post(url, payload_dict, timeout) -> response dict; injectable for tests.
PostFn = Callable[[str, dict, float], dict]
GetFn = Callable[[str, float], dict]

CONTRADICTIONS = re.compile(
    r"\b(don'?t buy|do not buy|avoid|short(?:-| )?sell|sell (?:it|now|this)|stay away|not a good buy)\b", re.I)
NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _http_get(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_post(url: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def ollama_status(cfg: Config, get: GetFn = _http_get, timeout: float = 2.0) -> tuple[bool, str]:
    """(usable, reason). Usable means Ollama answers and the configured model is installed."""
    if not cfg.explainer.enabled:
        return False, "explainer disabled in config.yaml"
    try:
        tags = get(cfg.explainer.ollama_url.rstrip("/") + "/api/tags", timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as err:
        return False, f"Ollama not reachable at {cfg.explainer.ollama_url} ({err})"
    names = {m.get("name", "") for m in tags.get("models", [])}
    want = cfg.explainer.model
    if want in names or f"{want}:latest" in names:
        return True, f"using {want}"
    return False, f"model {want!r} not installed (have: {', '.join(sorted(names)) or 'none'}); " \
                  f"run `ollama pull {want}` or change explainer.model"


def signal_facts(sig: Signal, regime: RegimeState | None) -> dict:
    """The only information the model may use, with numbers pre-formatted."""
    facts = {
        "stock": f"{sig.symbol} ({sig.name})" if sig.name else sig.symbol,
        "sector": sig.sector,
        "decision_by_rules": sig.status + (f" ({sig.status_note})" if sig.status_note else ""),
        "buy_zone": f"{inr(sig.buy_zone_low)} to {inr(sig.buy_zone_high)}, valid on the next trading day only",
        "stop_loss": inr(sig.stop),
        "target": inr(sig.target),
        "reward_to_risk": f"{sig.rr:.1f}",
        "reasons": sig.reasons,
        "warnings": sig.warnings,
    }
    if sig.qty:
        facts["quantity"] = str(sig.qty)
        facts["rupees_at_risk"] = inr(sig.risk_amount)
    if regime is not None:
        facts["market"] = f"{regime.regime}: {regime.description}"
    return facts


def build_prompt(facts: dict) -> str:
    """Prompt that asks for a short explanation and forbids new numbers or advice."""
    return (
        "You explain a swing-trade setup that a fixed, rule-based scanner has already found, "
        "for a retail investor in India.\n"
        "Write 3 to 4 plain-English sentences: what the setup is, why the rules flagged it, "
        "and what the buy zone, stop-loss and target mean.\n"
        "Strict rules:\n"
        "- Use ONLY the facts below. Do not add outside knowledge, news or predictions.\n"
        "- Copy any number exactly as written in the facts. Never compute, round or invent numbers.\n"
        "- Do not give a different recommendation or any advice of your own. Do not tell the reader to "
        "avoid, sell or skip the trade.\n"
        "- No headings, lists or disclaimers. Plain sentences only.\n\n"
        f"FACTS (JSON):\n{json.dumps(facts, ensure_ascii=False, indent=1)}\n\nExplanation:"
    )


def _numbers(text: str) -> list[float]:
    out = []
    for m in NUMBER.findall(text):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def is_faithful(text: str, facts: dict, cfg: Config) -> tuple[bool, str]:
    """Reject replies with numbers absent from the facts or wording that contradicts the signal."""
    if not text or len(text) < 40:
        return False, "empty or too short"
    if CONTRADICTIONS.search(text):
        return False, "contradicts the rules' decision"
    allowed = set(_numbers(json.dumps(facts, ensure_ascii=False)))
    e, st = cfg.entry, cfg.stops_targets
    allowed |= {float(x) for x in (e.ema_short, e.ema_mid, e.ema_long, e.rsi_period, e.pullback_lookback,
                                   st.target_r, 1, 2, 3, 4)}
    for n in _numbers(text):
        if not any(abs(n - a) <= max(0.01, abs(a) * 0.005) for a in allowed):
            return False, f"number {n:g} is not in the facts"
    return True, ""


def explain_signal(sig: Signal, regime: RegimeState | None, cfg: Config,
                   post: PostFn = _http_post, timeout: float = 60.0) -> str | None:
    """One explanation, or None (caller falls back to the rule-based reasons)."""
    facts = signal_facts(sig, regime)
    payload = {"model": cfg.explainer.model, "prompt": build_prompt(facts), "stream": False,
               "options": {"temperature": 0.1, "num_predict": 220}}
    try:
        reply = post(cfg.explainer.ollama_url.rstrip("/") + "/api/generate", payload, timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as err:
        log.info("Explainer: %s failed (%s); using rule-based reasons", sig.symbol, err)
        return None
    text = " ".join(str(reply.get("response", "")).split())
    ok, why = is_faithful(text, facts, cfg)
    if not ok:
        log.info("Explainer: rejected reply for %s (%s); using rule-based reasons", sig.symbol, why)
        return None
    return text


def explain_signals(signals: list[Signal], regime: RegimeState | None, cfg: Config,
                    get: GetFn = _http_get, post: PostFn = _http_post,
                    budget_seconds: float = 180.0) -> tuple[dict[str, str], str]:
    """Explanations keyed by symbol (TAKE first), plus a status line. Never raises."""
    usable, reason = ollama_status(cfg, get)
    if not usable:
        log.info("Explainer off: %s", reason)
        return {}, reason
    out: dict[str, str] = {}
    start = time.monotonic()
    ordered = sorted(signals, key=lambda s: s.status != "TAKE")
    for s in ordered:
        left = budget_seconds - (time.monotonic() - start)
        if left <= 5:
            log.info("Explainer: time budget used up; remaining signals keep rule-based reasons")
            break
        try:
            text = explain_signal(s, regime, cfg, post, timeout=min(60.0, left))
        except Exception as err:  # never let the LLM break the run
            log.info("Explainer: unexpected error for %s: %s", s.symbol, err)
            text = None
        if text:
            out[s.symbol] = text
    return out, f"{reason}; explained {len(out)}/{len(signals)} signal(s)"
