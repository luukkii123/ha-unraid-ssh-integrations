"""Temperature and fan entities against real Home Assistant platforms."""
from dataclasses import replace
from pathlib import Path

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, REVOLUTIONS_PER_MINUTE, UnitOfTemperature
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unraid_ssh import parse
from custom_components.unraid_ssh.parse import Gpu
from test_device_migration import inventory, load, own_entities

RECORDED = parse.parse_sensors(
    (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sensors.tsv").read_text(encoding="utf-8")
)


def snapshot_with_sensors(sensors=None, **changes):
    """The migration inventory plus the recorded hwmon channels."""
    return replace(
        inventory(),
        sensors=tuple(RECORDED if sensors is None else sensors),
        gpus=(Gpu(0, "Example GPU", 10, 20, 100, 20.0, 40, 12.0, 31),),
        **changes,
    )


async def setup(hass, monkeypatch, tmp_path, snapshot):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain="unraid_ssh", title="Example Unraid", data={"host": "example.invalid"})
    entry.add_to_hass(hass)
    await load(hass, monkeypatch, entry, snapshot)
    return entry


def state_of(hass, entry, suffix):
    item = own_entities(hass, entry)["_" + suffix]
    return hass.states.get(item.entity_id)


async def test_every_channel_becomes_an_entity_with_a_stable_id(hass, monkeypatch, tmp_path):
    entry = await setup(hass, monkeypatch, tmp_path, snapshot_with_sensors())
    try:
        suffixes = set(own_entities(hass, entry))
        assert "_temp_k10temp_0000_00_18_3_temp1" in suffixes      # Tctl
        assert "_temp_nct6798_nct6775_656_temp1" in suffixes       # SYSTIN
        assert "_temp_nvme_nvme0_temp1" in suffixes                # Composite
        assert "_fan_nct6798_nct6775_656_fan4_rpm" in suffixes
        assert "_fan_nct6798_nct6775_656_fan4_percent" in suffixes
        # fan7 reports no pwm at all: speed only, no drive level.
        assert "_fan_nct6798_nct6775_656_fan7_rpm" in suffixes
        assert "_fan_nct6798_nct6775_656_fan7_percent" not in suffixes
        # AUXTIN2 (-59 °C) and the four PCH_* channels (0 °C) are unwired inputs.
        for channel in ("temp5", "temp9", "temp10", "temp11", "temp12"):
            assert f"_temp_nct6798_nct6775_656_{channel}" not in suffixes
        assert not any("hwmon" in suffix for suffix in suffixes)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_only_the_meaningful_channels_are_enabled_by_default(hass, monkeypatch, tmp_path):
    entry = await setup(hass, monkeypatch, tmp_path, snapshot_with_sensors())
    try:
        items = own_entities(hass, entry)
        enabled = {
            "_temp_k10temp_0000_00_18_3_temp1",       # Tctl
            "_temp_nct6798_nct6775_656_temp1",        # SYSTIN
            "_temp_nct6798_nct6775_656_temp2",        # CPUTIN
            "_temp_nvme_nvme0_temp1",                 # Composite
            "_fan_nct6798_nct6775_656_fan4_rpm",      # 874 RPM
            "_fan_nct6798_nct6775_656_fan6_rpm",      # 3409 RPM
        }
        disabled = {
            "_temp_k10temp_0000_00_18_3_temp3",       # Tccd1
            "_temp_nvme_nvme0_temp2",                 # Sensor 1
            "_temp_nct6798_nct6775_656_temp3",        # AUXTIN0
            "_fan_nct6798_nct6775_656_fan1_rpm",      # empty header
            "_fan_nct6798_nct6775_656_fan1_percent",
        }
        for suffix in enabled:
            assert items[suffix].disabled_by is None, suffix
        for suffix in disabled:
            assert items[suffix].disabled_by is er.RegistryEntryDisabler.INTEGRATION, suffix
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_values_units_and_attributes_match_the_recording(hass, monkeypatch, tmp_path):
    entry = await setup(hass, monkeypatch, tmp_path, snapshot_with_sensors())
    try:
        tctl = state_of(hass, entry, "temp_k10temp_0000_00_18_3_temp1")
        assert tctl.state == "71.8"
        assert tctl.attributes["unit_of_measurement"] == UnitOfTemperature.CELSIUS
        assert tctl.attributes["device_class"] == SensorDeviceClass.TEMPERATURE
        assert tctl.attributes["state_class"] == SensorStateClass.MEASUREMENT
        # Precision lives in the registry options, not in the state attributes.
        options = own_entities(hass, entry)["_temp_k10temp_0000_00_18_3_temp1"].options
        assert options["sensor"]["suggested_display_precision"] == 1
        assert (tctl.attributes["chip"], tctl.attributes["channel"], tctl.attributes["label"]) == (
            "k10temp", "temp1", "Tctl")
        assert tctl.name == "Example Unraid Temperature CPU Tctl"

        rpm = state_of(hass, entry, "fan_nct6798_nct6775_656_fan4_rpm")
        assert rpm.state == "874"
        assert rpm.attributes["unit_of_measurement"] == REVOLUTIONS_PER_MINUTE
        assert "device_class" not in rpm.attributes
        assert rpm.attributes["icon"] == "mdi:fan"
        assert rpm.name == "Example Unraid Fan 4 speed"

        percent = state_of(hass, entry, "fan_nct6798_nct6775_656_fan4_percent")
        assert percent.state == "90"                 # 229 / 255
        assert percent.attributes["unit_of_measurement"] == PERCENTAGE
        assert (percent.attributes["pwm"], percent.attributes["pwm_mode"]) == (229, "auto")
        assert percent.name == "Example Unraid Fan 4 power"

        # The NVMe label alone ("Composite") says nothing; the chip name does.
        composite = state_of(hass, entry, "temp_nvme_nvme0_temp1")
        assert composite.name == "Example Unraid Temperature NVMe Composite"
        assert composite.state == "51.9"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_manually_driven_fan_says_so(hass, monkeypatch, tmp_path):
    """fan5 runs on a fixed level, not on the board's automatic curve."""
    manual = [s for s in RECORDED if s.channel in ("fan5",)]
    entry = await setup(hass, monkeypatch, tmp_path, snapshot_with_sensors(manual))
    try:
        item = own_entities(hass, entry)["_fan_nct6798_nct6775_656_fan5_percent"]
        er.async_get(hass).async_update_entity(item.entity_id, disabled_by=None)
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        state = state_of(hass, entry, "fan_nct6798_nct6775_656_fan5_percent")
        assert (state.state, state.attributes["pwm_mode"]) == ("60", "manual")
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_channel_that_leaves_the_plausible_range_keeps_its_entity(hass, monkeypatch, tmp_path):
    entry = await setup(hass, monkeypatch, tmp_path, snapshot_with_sensors())
    try:
        tctl = next(s for s in RECORDED if s.label == "Tctl")
        broken = [replace(tctl, value=-59.0) if s is tctl else s for s in RECORDED]
        entry.runtime_data.coordinator.async_set_updated_data(snapshot_with_sensors(broken))
        await hass.async_block_till_done()
        assert "_temp_k10temp_0000_00_18_3_temp1" in own_entities(hass, entry)
        assert state_of(hass, entry, "temp_k10temp_0000_00_18_3_temp1").state == "unknown"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_channel_that_disappears_goes_unavailable(hass, monkeypatch, tmp_path):
    entry = await setup(hass, monkeypatch, tmp_path, snapshot_with_sensors())
    try:
        entry.runtime_data.coordinator.async_set_updated_data(snapshot_with_sensors(()))
        await hass.async_block_till_done()
        assert state_of(hass, entry, "temp_k10temp_0000_00_18_3_temp1").state == "unavailable"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_the_two_fixed_server_temperatures_exist_without_any_chip(hass, monkeypatch, tmp_path):
    entry = await setup(hass, monkeypatch, tmp_path, snapshot_with_sensors())
    try:
        assert state_of(hass, entry, "cpu_temp").state == "71.8"      # k10temp Tctl
        assert state_of(hass, entry, "board_temp").state == "43.0"    # nct6798 SYSTIN
        entry.runtime_data.coordinator.async_set_updated_data(snapshot_with_sensors(()))
        await hass.async_block_till_done()
        # Created like cpu_percent, whether or not a source exists.
        assert state_of(hass, entry, "cpu_temp").state == "unknown"
        assert state_of(hass, entry, "board_temp").state == "unknown"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("fan_percent, expected", [(31, "31"), (None, None)])
async def test_the_gpu_fan_exists_only_where_the_driver_reports_one(
    hass, monkeypatch, tmp_path, fan_percent, expected
):
    gpu = Gpu(0, "Example GPU", 10, 20, 100, 20.0, 40, 12.0, fan_percent)
    entry = await setup(hass, monkeypatch, tmp_path, replace(snapshot_with_sensors(), gpus=(gpu,)))
    try:
        items = own_entities(hass, entry)
        assert ("_gpu_fan_0" in items) is (expected is not None)
        if expected is not None:
            state = state_of(hass, entry, "gpu_fan_0")
            assert state.state == expected
            assert state.attributes["unit_of_measurement"] == PERCENTAGE
            assert state.attributes["icon"] == "mdi:fan"
            assert state.name == "Example Unraid GPU 0 Example GPU fan"
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
