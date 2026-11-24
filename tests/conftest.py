from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from swing_agent import db  # noqa: E402
from swing_agent.config import load_config  # noqa: E402


@pytest.fixture
def cfg():
    """The project's config, with a fixed ₹1,00,000 capital so tests don't depend on your own setting."""
    c = load_config(ROOT / "config.yaml")
    c.account.capital = 100000
    return c


@pytest.fixture
def conn(tmp_path):
    """A fresh SQLite DB in a temp folder."""
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()
