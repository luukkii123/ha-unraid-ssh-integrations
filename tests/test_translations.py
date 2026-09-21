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
    """Every container update names its container on the shared device."""
    entity = _load("strings.json")["entity"]
    assert "container_update" in entity["update"]
    assert "updates_available" in entity["sensor"]


def test_device_type_names_are_translatable():
    expected = {
        "strings.json": {
            "disk": "{prefix} Disk {component}",
            "stack": "{prefix} Stack {component}",
            "vm": "{prefix} VM {component}",
            "standalone_containers": "{prefix} Standalone containers",
            "share": "{prefix} Share {component}",
        },
        "translations/en.json": {
            "disk": "{prefix} Disk {component}",
            "stack": "{prefix} Stack {component}",
            "vm": "{prefix} VM {component}",
            "standalone_containers": "{prefix} Standalone containers",
            "share": "{prefix} Share {component}",
        },
        "translations/de.json": {
            "disk": "{prefix} Festplatte {component}",
            "stack": "{prefix} Stack {component}",
            "vm": "{prefix} VM {component}",
            "standalone_containers": "{prefix} Freie Container",
            "share": "{prefix} Share {component}",
        },
    }

    for filename, names in expected.items():
        assert {key: value["name"] for key, value in _load(filename)["device"].items()} == {**names, "container": "{prefix} {component}"}


def test_grouped_entity_names_include_the_component_once():
    expected = {
        "strings.json": ("Used", "Free", "GPU {index} {gpu} utilization", "{container} update"),
        "translations/en.json": ("Used", "Free", "GPU {index} {gpu} utilization", "{container} update"),
        "translations/de.json": ("Belegt", "Frei", "GPU {index} {gpu} Auslastung", "{container} Update"),
    }

    for filename, names in expected.items():
        entity = _load(filename)["entity"]
        assert (
            entity["sensor"]["share_used"]["name"],
            entity["sensor"]["share_free"]["name"],
            entity["sensor"]["gpu_util"]["name"],
            entity["update"]["container_update"]["name"],
        ) == names


def test_hardware_sensor_keys_are_translated_in_both_languages():
    """Every new entity needs a name in all three files (docs/ui-regeln.md)."""
    expected = {
        "strings.json": {
            "cpu_temp": "CPU temperature",
            "board_temp": "Board temperature",
            "hw_temp": "Temperature {chip} {label}",
            "fan_rpm": "Fan {label} speed",
            "fan_percent": "Fan {label} power",
            "gpu_fan": "GPU {index} {gpu} fan",
        },
        "translations/en.json": {
            "cpu_temp": "CPU temperature",
            "board_temp": "Board temperature",
            "hw_temp": "Temperature {chip} {label}",
            "fan_rpm": "Fan {label} speed",
            "fan_percent": "Fan {label} power",
            "gpu_fan": "GPU {index} {gpu} fan",
        },
        "translations/de.json": {
            "cpu_temp": "CPU-Temperatur",
            "board_temp": "Mainboard-Temperatur",
            "hw_temp": "Temperatur {chip} {label}",
            "fan_rpm": "L\u00fcfter {label} Drehzahl",
            "fan_percent": "L\u00fcfter {label} Leistung",
            "gpu_fan": "GPU {index} {gpu} L\u00fcfter",
        },
    }
    for filename, names in expected.items():
        sensor = _load(filename)["entity"]["sensor"]
        assert {key: sensor[key]["name"] for key in names} == names, filename


def test_hardware_sensor_placeholders_match_what_the_platform_supplies():
    """A placeholder the entity does not fill shows up raw in the entity name.

    Only the shape is checked here -- this suite stays free of Home Assistant.
    That the platform really supplies these values is measured end to end in
    `tests_ha/test_hw_sensors.py`, which asserts the rendered entity names.
    """
    import re

    supplied = {
        "hw_temp": {"chip", "label"},
        "fan_rpm": {"label"},
        "fan_percent": {"label"},
        "gpu_fan": {"gpu", "index"},
        "cpu_temp": set(),
        "board_temp": set(),
    }
    for filename in ("strings.json", "translations/en.json", "translations/de.json"):
        entity = _load(filename)["entity"]["sensor"]
        for key, names in supplied.items():
            assert set(re.findall(r"{(\w+)}", entity[key]["name"])) == names, (filename, key)


def test_icons_exist_for_the_new_keys_without_a_device_class():
    from unraid_ssh import const

    assert const.ENTITY_ICONS["fan_rpm"] == "mdi:fan"
    assert const.ENTITY_ICONS["fan_percent"] == "mdi:fan"
    assert const.ENTITY_ICONS["gpu_fan"] == "mdi:fan"
