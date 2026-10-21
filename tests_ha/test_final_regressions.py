"""Transport and first-cache failures found during the final branch review."""
from dataclasses import replace
import json
import shlex
import subprocess
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import async_get_platforms
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unraid_ssh import collect
from custom_components.unraid_ssh.icon_cache import ContainerIconCache
from custom_components.unraid_ssh.model import ImageStatus, build_snapshot
from custom_components.unraid_ssh.ssh import CommandResult
from test_icon_cache import PictureSSH, snapshot
from test_device_migration import inventory, legacy, load, lookup, own_entities


@pytest.mark.parametrize('failure', ['reader_nonzero', 'entry_symlink', 'write_permission'])
async def test_initial_cache_failure_publishes_fallback_and_retries(hass, tmp_path, monkeypatch, failure):
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH()
    cache = ContainerIconCache(hass, 'entry-one', client)
    original_run, original_write = client.run, cache._write
    entry_dir = tmp_path / 'www/unraid_ssh/entry-one'
    if failure == 'reader_nonzero':
        client.run = AsyncMock(return_value=CommandResult(1, '', ''))
    elif failure == 'entry_symlink':
        outside = tmp_path / 'outside'
        outside.mkdir()
        entry_dir.parent.mkdir(parents=True)
        entry_dir.symlink_to(outside, target_is_directory=True)
    else:
        def denied(*args):
            raise PermissionError('synthetic entry directory denial')
        monkeypatch.setattr(cache, '_write', denied)
    changes = []
    cache.async_add_listener(lambda: changes.append(cache.picture('web')))
    state = snapshot(url='https://example.com/fallback.png')
    try:
        cache.schedule(state)
        await hass.async_block_till_done()
        assert cache.picture('web') == 'https://example.com/fallback.png'
        assert changes == ['https://example.com/fallback.png']
        client.run = original_run
        monkeypatch.setattr(cache, '_write', original_write)
        if failure == 'entry_symlink':
            entry_dir.unlink()
            assert list(outside.iterdir()) == []
        cache.schedule(state)  # Same revision must be retried after transient failure.
        await hass.async_block_till_done()
        assert cache.picture('web').startswith('/local/unraid_ssh/entry-one/')
        assert len(changes) == 2
    finally:
        await cache.async_shutdown()


@pytest.mark.parametrize('stop', ['stale', 'unload'])
async def test_failed_read_does_not_publish_stale_fallback(hass, tmp_path, stop):
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH()
    client.error = True
    client.release.clear()
    cache = ContainerIconCache(hass, 'entry-one', client)
    changes = []
    cache.async_add_listener(lambda: changes.append(cache.picture('web')))
    cache.schedule(snapshot(url='https://example.com/old.png'))
    await client.started.wait()
    if stop == 'stale':
        cache.schedule(snapshot(sources=False, url='https://example.com/new.png'))
    else:
        await cache.async_shutdown()
    client.release.set()
    await hass.async_block_till_done()
    assert 'https://example.com/old.png' not in changes
    assert cache.picture('web') == ('https://example.com/new.png' if stop == 'stale' else None)
    await cache.async_shutdown()


@pytest.mark.parametrize('occupant', ['empty', 'unknown_own', 'foreign'])
async def test_absent_legacy_without_matching_entity_creates_no_collection(hass, monkeypatch, tmp_path, occupant):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain='unraid_ssh', title='Example Unraid', data={'host': 'example.invalid'})
    entry.add_to_hass(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    old = devices.async_get_or_create(config_entry_id=entry.entry_id,
        identifiers={('unraid_ssh', entry.entry_id + '_container_removed')}, name='Removed')
    if occupant != 'empty':
        item = entities.async_get_or_create('switch', 'unraid_ssh' if occupant == 'unknown_own' else 'other',
            entry.entry_id + ('_unknown' if occupant == 'unknown_own' else '_container_removed'),
            config_entry=entry, device_id=old.id)
    await load(hass, monkeypatch, entry, replace(inventory(), containers=(), template_containers=()))
    try:
        assert lookup(hass, entry, '_containers') is None
        assert devices.async_get(old.id) is not None
        if occupant != 'empty':
            assert entities.async_get(item.entity_id) == item
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


def collected_snapshot(monkeypatch, failure=None):
    data = {
        'docker': 'alpha_one\trunning\texample/a\tproject\tweb\t""\n',
        'compose': json.dumps([{'Name': 'project', 'Status': 'running(1)', 'ConfigFiles': '/stacks/My Stack/compose.yaml'}]),
        'stacks': '/stacks/My Stack/\tMy Stack\tno\n',
        'icons': '{}',
    }
    commands = [(name, '(exit 7)' if name == failure else 'printf %s ' + shlex.quote(
        'not json' if name == 'compose' and failure == 'invalid_compose' else value)) for name, value in data.items()]
    monkeypatch.setattr(collect, 'SECTIONS', tuple(commands))
    completed = subprocess.run(['/bin/sh', '-c', collect.build_state_command()], capture_output=True, text=True, check=True)
    return build_snapshot(collect.split_output(completed.stdout), None)


@pytest.mark.parametrize('failure', ['docker', 'compose', 'stacks', 'invalid_compose'])
async def test_transport_failure_preserves_assignment_and_recovers_without_reload(hass, monkeypatch, tmp_path, failure):
    hass.config.config_dir = str(tmp_path)
    entry, _, old = legacy(hass)
    entities = er.async_get(hass)
    previous = old.alpha if failure == 'docker' else lookup(hass, entry, '_stack_My Stack')
    before = own_entities(hass, entry)
    tracked = ('_container_alpha_one', '_update_alpha_one')
    for suffix in tracked:
        entities.async_update_entity(before[suffix].entity_id, device_id=previous.id)
    failed_snapshot = collected_snapshot(monkeypatch, failure)
    assert ('compose' if failure == 'invalid_compose' else failure) in failed_snapshot.failed
    images = {'alpha_one': ImageStatus('alpha_one', 'example/a', 'sha256:old', 'sha256:new', True)}
    await load(hass, monkeypatch, entry, failed_snapshot, images)
    try:
        loaded = [item for platform in async_get_platforms(hass, 'unraid_ssh') for item in platform.entities.values()
                  if item.entity_id in {before[suffix].entity_id for suffix in tracked}]
        assert len(loaded) == (0 if failure == 'docker' else 2)
        assert all(own_entities(hass, entry)[suffix].device_id == previous.id for suffix in tracked)
        assert all(item.device_info['identifiers'] == previous.identifiers for item in loaded)
        assert lookup(hass, entry, '_stack_project') is None
        entry.runtime_data.coordinator.async_set_updated_data(collected_snapshot(monkeypatch))
        await hass.async_block_till_done()
        stable = lookup(hass, entry, '_stack_My Stack')
        loaded = [item for platform in async_get_platforms(hass, 'unraid_ssh') for item in platform.entities.values()
                  if item.entity_id in {before[suffix].entity_id for suffix in tracked}]
        assert len(loaded) == 2
        assert all(own_entities(hass, entry)[suffix].device_id == stable.id for suffix in tracked)
        assert all(item.device_info['identifiers'] == stable.identifiers for item in loaded)
        entry.runtime_data.coordinator.async_set_updated_data(failed_snapshot)
        await hass.async_block_till_done()
        for suffix in tracked:
            current = own_entities(hass, entry)[suffix]
            assert (current.entity_id, current.unique_id, current.device_id) == (before[suffix].entity_id, before[suffix].unique_id, stable.id)
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('failed_section', ['containers', 'images'])
async def test_inventory_failure_keeps_last_update_state_and_recovers(hass, failed_section):
    from custom_components.unraid_ssh.coordinator import UpdateCoordinator, UpdateState
    entry = MockConfigEntry(domain='unraid_ssh', title='Example Unraid', data={'host': 'example.invalid'})
    entry.add_to_hass(hass)
    class CollectedSSH:
        failed = True
        async def run(self, command, *, timeout):
            if command == collect.build_inventory_command():
                commands = tuple((name, 'exit 7' if self.failed and name == failed_section else 'true')
                                 for name in ('containers', 'images'))
                command = collect._build_sections(commands)
            else:
                assert command == collect.build_remote_digest_command([])
            result = subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True, check=True)
            return CommandResult(0, result.stdout, '')
    client = CollectedSSH()
    coordinator = UpdateCoordinator(hass, entry, client)
    previous = UpdateState({'alpha_one': ImageStatus('alpha_one', 'example/a', 'sha256:old', 'sha256:new', True)}, None)
    coordinator.async_set_updated_data(previous)
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.data is previous
    client.failed = False
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data.images == {}
