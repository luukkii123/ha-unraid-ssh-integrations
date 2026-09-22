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


def stack_with_member(up=True):
    """A stack folder with one canonical Compose member, running or down.

    `docker compose down` removes the containers outright, so a stack that is
    down has its folder and no containers at all -- not stopped members.
    """
    from custom_components.unraid_ssh.model import merge_stacks
    from custom_components.unraid_ssh.parse import ComposeProject, Container, StackDir
    base = inventory()
    member = Container('stack-web-1', 'running', 'example/web', 'project', 'web', replica=1)
    containers = (member, base.containers[1]) if up else (base.containers[1],)
    projects = [ComposeProject('project', 'running(1)', 1, ('/stacks/My Stack/compose.yaml',))] if up else []
    stacks, loose = merge_stacks([StackDir('/stacks/My Stack', 'My Stack', False)], projects, list(containers))
    return replace(base, containers=containers, stacks=stacks, template_containers=loose)


MEMBER = '_container_compose:["My Stack","web",1]'


async def test_a_deleted_standalone_container_is_removed_after_grace(hass, monkeypatch, tmp_path, freezer):
    """The one-offs of the real server: gone from `docker ps -a`, gone for good."""
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        device = lookup(hass, entry, '_container_beta')
        gone = without(inventory(), 'beta')
        await poll(hass, entry, gone)
        freezer.tick(STALE_GRACE / 2)
        await poll(hass, entry, gone)
        assert '_container_beta' in own_entities(hass, entry)      # inside the window
        freezer.tick(STALE_GRACE)
        await poll(hass, entry, gone)
        current = own_entities(hass, entry)
        for suffix in ('_container_beta', '_update_beta', '_restart_container_beta'):
            assert suffix not in current, suffix
        assert dr.async_get(hass).async_get(device.id) is None
        assert '_container_alpha_one' in current                     # still on the server
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_member_of_a_stack_that_is_down_is_kept_and_returns(hass, monkeypatch, tmp_path, freezer):
    """Switching a stack off must not cost its members their entities.

    Their labels (`always_on`), areas and entity ids hang on those registry
    entries; a stack that comes back up has to find them where it left them.
    """
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, stack_with_member())
    try:
        before = own_entities(hass, entry)[MEMBER]
        await poll(hass, entry, stack_with_member(up=False))
        freezer.tick(STALE_GRACE * 3)
        await poll(hass, entry, stack_with_member(up=False))
        assert own_entities(hass, entry)[MEMBER] == before
        assert hass.states.get(before.entity_id).state == 'unavailable'
        await poll(hass, entry, stack_with_member())
        assert own_entities(hass, entry)[MEMBER] == before
        assert hass.states.get(before.entity_id).state == 'on'
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_a_failed_docker_poll_removes_nothing_itself(hass, monkeypatch, tmp_path, freezer):
    """A poll that could not read `docker` is no evidence a container is gone."""
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory())
    try:
        gone = without(inventory(), 'beta')
        failed = replace(gone, failed=frozenset({'docker'}))
        await poll(hass, entry, failed)
        freezer.tick(STALE_GRACE * 3)
        await poll(hass, entry, failed)
        assert '_container_beta' in own_entities(hass, entry)
        assert '_update_beta' in own_entities(hass, entry)
        # The clock only starts on a poll that could judge: a full window of
        # successful polls is still owed before anything goes.
        await poll(hass, entry, gone)
        assert '_container_beta' in own_entities(hass, entry)
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
