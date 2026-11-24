import time
import urllib.error
from datetime import date

import pytest

from swing_agent.explainer import explain_signals, is_faithful, ollama_status, signal_facts
from swing_agent.scanner import Signal


def _sig(status="TAKE"):
    s = Signal("TECHM", "Tech Mahindra", "IT", date(2026, 11, 23), 1777.5, 1777.5, 1817.74, 1690.01, 2073.22,
               127.73, 7.0, 2.0, 12.0, 4.0, 40.0, 1700.0,
               reasons=["Pulled back 6.2% from 20-day high (₹1,850.00)", "RSI dipped to 41 and is turning up (now 47)"])
    s.status, s.qty, s.risk_amount = status, 3, 383.21
    return s


def _cfg(cfg, enabled=True):
    c = cfg.model_copy(deep=True)
    c.explainer.enabled = enabled
    c.explainer.model = "llama3.2:3b"
    return c


def tags_ok(url, timeout):
    return {"models": [{"name": "llama3.2:3b"}]}


GOOD = ("TECHM pulled back 6.2% from its 20-day high and RSI dipped to 41 before turning up, so the rules "
        "flagged a pullback in an uptrend. The buy zone is ₹1,777.50 to ₹1,817.74 for the next trading day, "
        "with a stop-loss at ₹1,690.01 and a target of ₹2,073.22.")


def test_explains_when_ollama_answers(cfg):
    sent = {}

    def post(url, payload, timeout):
        sent.update(payload)
        return {"response": GOOD}

    texts, status = explain_signals([_sig()], None, _cfg(cfg), get=tags_ok, post=post)
    assert texts == {"TECHM": GOOD} and "explained 1/1" in status
    assert sent["model"] == "llama3.2:3b" and sent["stream"] is False
    assert "Copy any number exactly" in sent["prompt"] and "₹1,690.01" in sent["prompt"]


def test_disabled_does_nothing(cfg):
    def boom(*a):
        raise AssertionError("should not call Ollama")

    assert explain_signals([_sig()], None, _cfg(cfg, enabled=False), get=boom, post=boom)[0] == {}


def test_ollama_down_falls_back_quickly(cfg):
    def down(url, timeout):
        raise urllib.error.URLError("connection refused")

    t = time.monotonic()
    texts, status = explain_signals([_sig()], None, _cfg(cfg), get=down)
    assert texts == {} and "not reachable" in status
    assert time.monotonic() - t < 1


def test_missing_model_falls_back(cfg):
    ok, why = ollama_status(_cfg(cfg), get=lambda u, t: {"models": [{"name": "mistral:7b"}]})
    assert not ok and "ollama pull llama3.2:3b" in why


def test_timeout_falls_back(cfg):
    def slow(url, payload, timeout):
        raise TimeoutError("timed out")

    assert explain_signals([_sig()], None, _cfg(cfg), get=tags_ok, post=slow)[0] == {}


@pytest.mark.parametrize("bad, why", [
    (GOOD.replace("₹1,690.01", "₹1,650.00"), "not in the facts"),       # changed number
    (GOOD + " It could rise 25% by March.", "not in the facts"),          # invented prediction
    (GOOD + " Honestly, I would avoid it.", "contradicts"),               # different recommendation
    ("Buy.", "too short"),
])
def test_unfaithful_replies_rejected(cfg, bad, why):
    facts = signal_facts(_sig(), None)
    ok, reason = is_faithful(bad, facts, _cfg(cfg))
    assert not ok and why in reason
    texts, _ = explain_signals([_sig()], None, _cfg(cfg), get=tags_ok,
                               post=lambda u, p, t: {"response": bad})
    assert texts == {}


def test_good_reply_is_faithful(cfg):
    assert is_faithful(GOOD, signal_facts(_sig(), None), _cfg(cfg)) == (True, "")


def test_unexpected_error_never_raises(cfg):
    def weird(url, payload, timeout):
        raise KeyError("odd")

    assert explain_signals([_sig()], None, _cfg(cfg), get=tags_ok, post=weird)[0] == {}
