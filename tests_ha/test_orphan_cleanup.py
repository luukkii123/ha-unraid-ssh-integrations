"""Removal of entities the server no longer has, against real HA registries."""
from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.util import dt as dt_util

from custom_components.unraid_ssh.const import STALE_GRACE
from custom_components.unraid_ssh.prune import can_remove_device
from test_device_migration import inventory, legacy, load, lookup, own_entities

DOMAIN = 'unraid_ssh'


def without(snapshot, *names):
    """The same snapshot with those containers gone from the server."""
    keep = tuple(c for c in snapshot.containers if c.name not in names)
    return replace(snapshot, containers=keep, template_containers=keep)


async def poll(hass, entry, snapshot):
    entry.runtime_data.coordinator.async_set_updated_data(snapshot)
    await hass.async_block_till_done()


async def test_absent_containers_keep_registry_and_device_after_grace(hass, monkeypatch, tmp_path, freezer):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        before = own_entities(hass, entry)
        device = lookup(hass, entry, '_container_beta')
        gone = without(inventory(), 'beta')
        await poll(hass, entry, gone)
        freezer.tick(STALE_GRACE * 3)
        await poll(hass, entry, gone)
        for suffix in ('_container_beta', '_update_beta'):
            assert own_entities(hass, entry)[suffix] == before[suffix]
        assert dr.async_get(hass).async_get(device.id) is not None
        assert entry.runtime_data.stale_since == {}
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_container_returns_to_existing_entity_after_long_absence(hass, monkeypatch, tmp_path, freezer):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        before = own_entities(hass, entry)['_container_alpha_one']
        await poll(hass, entry, without(inventory(), 'alpha_one'))
        freezer.tick(STALE_GRACE * 3)
        await poll(hass, entry, without(inventory(), 'alpha_one'))
        assert hass.states.get(before.entity_id).state == 'unavailable'
        await poll(hass, entry, inventory())
        assert own_entities(hass, entry)['_container_alpha_one'] == before
        assert hass.states.get(before.entity_id).state == 'on'
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_docker_poll_never_removes_legacy_container(hass, monkeypatch, tmp_path, freezer):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        gone = without(inventory(), 'beta')
        await poll(hass, entry, gone)
        freezer.tick(STALE_GRACE * 3)
        await poll(hass, entry, replace(gone, failed=frozenset({'docker'})))
        await poll(hass, entry, gone)
        assert '_container_beta' in own_entities(hass, entry)
        assert '_update_beta' in own_entities(hass, entry)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_server_level_and_unknown_suffixes_are_never_candidates(hass, monkeypatch, tmp_path, freezer):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        entities = er.async_get(hass)
        foreign = entities.async_get_or_create(
            'sensor', DOMAIN, entry.entry_id + '_something_new_in_a_later_version',
            config_entry=entry, device_id=lookup(hass, entry, '').id)
        await poll(hass, entry, inventory())
        freezer.tick(STALE_GRACE * 2)
        await poll(hass, entry, inventory())
        current = own_entities(hass, entry)
        for suffix in ('_cpu_percent', '_cpu_temp', '_board_temp', '_array_started',
                       '_check_updates', '_updates_available'):
            assert suffix in current, suffix
        assert entities.async_get(foreign.entity_id) is not None
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_vanished_hwmon_channel_is_removed_after_the_window(hass, monkeypatch, tmp_path, freezer):
    from test_hw_sensors import snapshot_with_sensors, RECORDED

    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, snapshot_with_sensors())
    try:
        assert '_temp_k10temp_0000_00_18_3_temp1' in own_entities(hass, entry)
        # The nct6775 module was unloaded: the board chip is gone, the CPU and
        # the drive remain.
        fewer = [s for s in RECORDED if s.chip != 'nct6798']
        await poll(hass, entry, snapshot_with_sensors(fewer))
        freezer.tick(STALE_GRACE * 2)
        await poll(hass, entry, snapshot_with_sensors(fewer))
        current = own_entities(hass, entry)
        assert '_temp_nct6798_nct6775_656_temp1' not in current
        assert '_fan_nct6798_nct6775_656_fan4_rpm' not in current
        assert '_temp_k10temp_0000_00_18_3_temp1' in current
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_failed_sensor_section_keeps_every_channel(hass, monkeypatch, tmp_path, freezer):
    from test_hw_sensors import snapshot_with_sensors

    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, snapshot_with_sensors())
    try:
        broken = snapshot_with_sensors((), failed=frozenset({'sensors'}))
        await poll(hass, entry, broken)
        freezer.tick(STALE_GRACE * 2)
        await poll(hass, entry, broken)
        assert '_temp_k10temp_0000_00_18_3_temp1' in own_entities(hass, entry)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('suffix, present, allowed', [
    ('', True, False),                      # the server device, never
    ('_vm_Guest', True, False),
    ('_disk_disk1', True, False),
    ('_stack_My Stack', True, False),
    ('_container_alpha_one', True, False),
    ('_vm_Guest', False, True),             # the VM is gone from the snapshot
    ('_disk_disk1', False, True),
    ('_container_alpha_one', False, True),
])
async def test_manual_device_removal_follows_the_snapshot(hass, monkeypatch, tmp_path, suffix, present, allowed):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    snapshot = inventory()
    if not present:
        snapshot = replace(snapshot, vms=(), disks=(), containers=(), template_containers=())
    await load(hass, monkeypatch, entry, snapshot)
    try:
        device = lookup(hass, entry, suffix)
        assert device is not None
        assert can_remove_device(entry, device) is allowed
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_manual_device_removal_is_refused_while_a_section_failed(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        device = lookup(hass, entry, '_vm_Guest')
        blind = replace(inventory(), vms=(), failed=frozenset({'vms'}))
        entry.runtime_data.coordinator.async_set_updated_data(blind)
        await hass.async_block_till_done()
        assert can_remove_device(entry, device) is False
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_foreign_device_is_never_removable(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        foreign = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={('other_integration', 'x')})
        assert can_remove_device(entry, foreign) is False
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
