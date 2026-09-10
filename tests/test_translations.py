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


def test_enum_states_match_the_constants():
    """Every ENUM sensor's options come from const; the texts must cover exactly
    those. A missing state shows as the raw value, a surplus one is dead text."""
    from unraid_ssh import const

    sensor = _load("strings.json")["entity"]["sensor"]
    for key, states in (
        ("array_state", const.ARRAY_STATES),
        ("disk_status", const.DISK_STATUSES),
        ("vm_state", const.VM_STATES),
    ):
        assert set(sensor[key]["state"]) == set(states), key


def test_switch_keys_are_translated():
    switch = _load("strings.json")["entity"]["switch"]
    assert {"container", "stack", "vm"} <= set(switch)


def test_update_and_counter_keys_are_translated():
    """Two names for the update entity: on a stack device it must say which
    container it belongs to, on the container's own device that would just
    repeat the device name. Both keys must exist in every file."""
    entity = _load("strings.json")["entity"]
    assert {"container_update", "image_update"} <= set(entity["update"])
    assert "updates_available" in entity["sensor"]

