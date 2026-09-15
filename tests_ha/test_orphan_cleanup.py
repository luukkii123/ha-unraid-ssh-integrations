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


async def test_an_absent_container_survives_the_grace_period_and_then_goes(hass, monkeypatch, tmp_path, freezer, caplog):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        assert '_container_beta' in own_entities(hass, entry)
        gone = without(inventory(), 'beta')
        await poll(hass, entry, gone)
        # First poll without it: the clock starts, nothing is removed.
        assert '_container_beta' in own_entities(hass, entry)
        assert entry.entry_id + '_container_beta' in entry.runtime_data.stale_since

        freezer.tick(STALE_GRACE - timedelta(seconds=30))
        await poll(hass, entry, gone)
        assert '_container_beta' in own_entities(hass, entry)

        caplog.clear()
        freezer.tick(timedelta(minutes=1))
        await poll(hass, entry, gone)
        current = own_entities(hass, entry)
        assert '_container_beta' not in current and '_update_beta' not in current
        assert '_container_alpha_one' in current       # the one still running stays
        assert 'absent for 5 min' in caplog.text
        assert entry.runtime_data.stale_since == {}
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_container_that_comes_back_within_the_window_keeps_its_entity(hass, monkeypatch, tmp_path, freezer):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        before = own_entities(hass, entry)['_container_beta']
        await poll(hass, entry, without(inventory(), 'beta'))
        freezer.tick(STALE_GRACE - timedelta(seconds=30))
        await poll(hass, entry, inventory())            # `compose up` finished
        assert entry.runtime_data.stale_since == {}
        freezer.tick(STALE_GRACE * 2)
        await poll(hass, entry, inventory())
        assert own_entities(hass, entry)['_container_beta'] == before
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_failed_section_neither_removes_nor_resets_the_clock(hass, monkeypatch, tmp_path, freezer):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        gone = without(inventory(), 'beta')
        await poll(hass, entry, gone)
        started = entry.runtime_data.stale_since[entry.entry_id + '_container_beta']

        # `docker ps` unreadable: the server said nothing about containers.
        freezer.tick(STALE_GRACE * 2)
        await poll(hass, entry, replace(gone, containers=(), template_containers=(),
                                        failed=frozenset({'docker'})))
        assert '_container_beta' in own_entities(hass, entry)
        assert entry.runtime_data.stale_since[entry.entry_id + '_container_beta'] == started

        # And the clock kept running, so the next readable poll removes it.
        await poll(hass, entry, gone)
        assert '_container_beta' not in own_entities(hass, entry)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_removed_container_returns_with_the_same_unique_id(hass, monkeypatch, tmp_path, freezer):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        unique_id = own_entities(hass, entry)['_container_beta'].unique_id
        await poll(hass, entry, without(inventory(), 'beta'))   # the clock starts here
        freezer.tick(STALE_GRACE * 2)
        await poll(hass, entry, without(inventory(), 'beta'))
        assert '_container_beta' not in own_entities(hass, entry)
        loaded = {item.unique_id for platform in async_get_platforms(hass, DOMAIN)
                  for item in platform.entities.values()}
        assert unique_id not in loaded

        await poll(hass, entry, inventory())            # the container is back
        item = own_entities(hass, entry)['_container_beta']
        assert item.unique_id == unique_id
        loaded = {i.unique_id for p in async_get_platforms(hass, DOMAIN) for i in p.entities.values()}
        assert unique_id in loaded
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_an_emptied_device_falls_but_the_server_device_never_does(hass, monkeypatch, tmp_path, freezer):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        collection = lookup(hass, entry, '_containers')
        server = lookup(hass, entry, '')
        assert collection is not None and server is not None
        empty = replace(inventory(), containers=(), template_containers=())
        await poll(hass, entry, empty)
        freezer.tick(STALE_GRACE * 2)
        await poll(hass, entry, empty)
        devices = dr.async_get(hass)
        assert devices.async_get(collection.id) is None
        assert devices.async_get(server.id) is not None
        assert own_entities(hass, entry)['_cpu_percent'].device_id == server.id
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
    ('_containers', True, False),
    ('_vm_Guest', False, True),             # the VM is gone from the snapshot
    ('_disk_disk1', False, True),
    ('_containers', False, True),
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
