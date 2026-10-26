"""Filesystem capacity and entity lifetime against real HA platforms."""
from dataclasses import replace
from pathlib import Path

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfInformation

from custom_components.unraid_ssh.const import STALE_GRACE
from custom_components.unraid_ssh.parse import parse_disks
from custom_components.unraid_ssh.sensor import expected_keys
from test_device_migration import inventory, legacy, load, lookup, own_entities
from test_orphan_cleanup import poll

CAPACITY = ("disk_total", "disk_used", "disk_free")


def filesystem(size="1000", free="250", device="sdb"):
    return tuple(parse_disks(
        f'["disk1"]\nname="disk1"\ndevice="{device}"\ntype="Data"\n'
        f'status="DISK_OK"\nfsSize="{size}"\nfsFree="{free}"\n'
    ))


async def setup(hass, monkeypatch, tmp_path, disks):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, replace(inventory(), disks=disks))
    return entry


def state(hass, entry, role, name="disk1"):
    return hass.states.get(own_entities(hass, entry)[f"_{role}_{name}"].entity_id)


async def test_mixed_disks_and_pool_capacity_uses_the_filesystem_owner_once(hass, monkeypatch, tmp_path):
    disks = tuple(parse_disks((Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "disks.ini").read_text()))
    entry = await setup(hass, monkeypatch, tmp_path, disks)
    try:
        for name, want in {
            "disk1": (25389180876, 23728080632, 1661100244),
            "boot_data": (454610044, 358379996, 96230048),
            "cache": (976761560, 440378636, 536382924),
            "raid": (7674263762, 3555552455, 4118711307),
        }.items():
            for role, value in zip(CAPACITY, want, strict=True):
                item = own_entities(hass, entry)[f"_{role}_{name}"]
                assert item.unique_id == f"{entry.entry_id}_{role}_{name}"
                assert item.device_id == lookup(hass, entry, f"_disk_{name}").id
                current = state(hass, entry, role, name)
                assert float(current.state) == value
                assert current.attributes["unit_of_measurement"] == UnitOfInformation.KIBIBYTES
                assert current.attributes["device_class"] == SensorDeviceClass.DATA_SIZE
                assert current.attributes["state_class"] == SensorStateClass.MEASUREMENT
        keys = expected_keys(entry.runtime_data.coordinator, entry.runtime_data.coordinator.data)
        for name in ("raid2", "raid3"):
            for role in CAPACITY:
                assert f"_{role}_{name}" not in own_entities(hass, entry)
                assert f"{role}_{name}" in keys
        assert "_disk_total_disk2" not in own_entities(hass, entry)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("size,free,want", [
    ("1000", "0", ("1000", "1000", "0")),
    ("1000", "1000", ("1000", "0", "1000")),
    ("1000", "", ("1000", "unknown", "unknown")),
    ("1000", "bad", ("1000", "unknown", "unknown")),
    ("1000", "-1", ("1000", "unknown", "unknown")),
    ("1000", "1001", ("1000", "unknown", "unknown")),
    ("0", "0", None),
    ("", "", None),
    ("bad", "0", None),
    ("-1000", "0", None),
])
async def test_capacity_never_fabricates_zero_for_invalid_filesystem_values(
    hass, monkeypatch, tmp_path, size, free, want
):
    entry = await setup(hass, monkeypatch, tmp_path, filesystem(size, free))
    try:
        items = own_entities(hass, entry)
        if want is None:
            assert all(f"_{role}_disk1" not in items for role in CAPACITY)
        else:
            assert tuple(state(hass, entry, role).state for role in CAPACITY) == want
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("size,free", [("", ""), ("0", "0"), ("bad", "0")])
async def test_lost_filesystem_keeps_capacity_keys_and_ids_then_recovers(hass, monkeypatch, tmp_path, freezer, size, free):
    entry = await setup(hass, monkeypatch, tmp_path, filesystem())
    try:
        original = own_entities(hass, entry)
        original_ids = {suffix: item.entity_id for suffix, item in original.items()}
        empty = replace(inventory(), disks=filesystem(size, free))
        await poll(hass, entry, empty)
        freezer.tick(STALE_GRACE * 2)
        await poll(hass, entry, empty)
        for role in CAPACITY:
            suffix = f"_{role}_disk1"
            assert own_entities(hass, entry)[suffix].entity_id == original_ids[suffix]
            assert state(hass, entry, role).state == "unknown"
        await poll(hass, entry, replace(inventory(), disks=filesystem("1000", "0")))
        assert tuple(state(hass, entry, role).state for role in CAPACITY) == ("1000", "1000", "0")
        assert all(own_entities(hass, entry)[suffix].entity_id == entity_id for suffix, entity_id in original_ids.items())
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_filesystem_that_appears_later_adds_capacity_without_replacing_disk_entities(
    hass, monkeypatch, tmp_path
):
    entry = await setup(hass, monkeypatch, tmp_path, filesystem("", ""))
    try:
        before = {suffix: item.entity_id for suffix, item in own_entities(hass, entry).items()}
        await poll(hass, entry, replace(inventory(), disks=filesystem()))
        assert tuple(state(hass, entry, role).state for role in CAPACITY) == ("1000", "750", "250")
        assert all(own_entities(hass, entry)[suffix].entity_id == entity_id for suffix, entity_id in before.items())
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("language,disk_word,names", [
    ("en", "Disk", ("Total space", "Used space", "Free space")),
    ("de", "Festplatte", ("Speicherplatz gesamt", "Speicherplatz belegt", "Speicherplatz frei")),
])
async def test_capacity_names_follow_the_home_assistant_language(
    hass, monkeypatch, tmp_path, language, disk_word, names
):
    hass.config.language = language
    entry = await setup(hass, monkeypatch, tmp_path, filesystem())
    try:
        assert tuple(state(hass, entry, role).name for role in CAPACITY) == tuple(
            f"Example Unraid {disk_word} disk1 {name}" for name in names
        )
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_capacity_goes_unavailable_and_prunes_only_after_successful_disk_absence(
    hass, monkeypatch, tmp_path, freezer
):
    entry = await setup(hass, monkeypatch, tmp_path, filesystem())
    try:
        ids = {role: own_entities(hass, entry)[f"_{role}_disk1"].entity_id for role in CAPACITY}
        failed = replace(inventory(), disks=(), failed=frozenset({"disks"}))
        await poll(hass, entry, failed)
        freezer.tick(STALE_GRACE * 2)
        await poll(hass, entry, failed)
        for role, entity_id in ids.items():
            assert f"_{role}_disk1" in own_entities(hass, entry)
            assert hass.states.get(entity_id).state == "unavailable"
        gone = replace(inventory(), disks=filesystem(device=""))
        await poll(hass, entry, gone)
        freezer.tick(STALE_GRACE * 2)
        await poll(hass, entry, gone)
        assert all(f"_{role}_disk1" not in own_entities(hass, entry) for role in CAPACITY)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
