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
    """The project's default config (validated)."""
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def conn(tmp_path):
    """A fresh SQLite DB in a temp folder."""
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()
