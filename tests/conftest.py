"""Shared test helpers: import path and fixture loading."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

COMPONENT = ROOT / "custom_components" / "unraid_ssh"

# The package __init__ pulls in Home Assistant, which this test image has no
# reason to install: everything under test here (parse, model, collect, ssh) is
# plain Python. So the package is registered by path only — the module object is
# created from its spec and never executed — and `from unraid_ssh import parse`
# keeps working, relative imports included.
if "unraid_ssh" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "unraid_ssh",
        COMPONENT / "__init__.py",
        submodule_search_locations=[str(COMPONENT)],
    )
    sys.modules["unraid_ssh"] = importlib.util.module_from_spec(_spec)

FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture
def fixture():
    """Return a loader: fixture("disks.ini") -> str."""

    def _load(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return _load
