"""strings.json, en.json and de.json must carry the same keys, and every
config-flow field needs a data_description (docs/ui-regeln.md, rule 3)."""

from __future__ import annotations

import json
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "unraid_ssh"


def _keys(node, prefix=""):
    out = set()
    for key, value in node.items():
        out.add(prefix + key)
        if isinstance(value, dict):
            out |= _keys(value, prefix + key + ".")
    return out


def _load(name):
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))


def test_translation_files_have_identical_keys():
    base = _keys(_load("strings.json"))
    assert _keys(_load("translations/en.json")) == base
    assert _keys(_load("translations/de.json")) == base


def test_every_data_field_has_a_description():
    strings = _load("strings.json")
    for flow in ("config", "options"):
        for step, body in strings[flow]["step"].items():
            data = body.get("data", {})
            desc = body.get("data_description", {})
            assert set(data) == set(desc), f"{flow}.{step}: data {set(data)} vs data_description {set(desc)}"
