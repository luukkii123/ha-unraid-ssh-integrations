"""Shared test helpers: import path and fixture loading."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def fixture():
    """Return a loader: fixture("disks.ini") -> str."""

    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _load
