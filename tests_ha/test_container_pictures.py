"""Real config entries, platforms, entity registry and HA HTTP serving."""
from unittest.mock import AsyncMock

from homeassistant.components.http import StaticPathConfig
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
import pytest

from custom_components.unraid_ssh.coordinator import UpdateState
from custom_components.unraid_ssh.model import ImageStatus
from test_icon_cache import PNG, SVG, PictureSSH, snapshot


async def setup_entry(hass, monkeypatch, client, initial=None):
    monkeypatch.setattr('custom_components.unraid_ssh.build_client', lambda entry: client)
    monkeypatch.setattr('custom_components.unraid_ssh.coordinator.UnraidCoordinator._async_update_data', AsyncMock(return_value=initial if initial is not None else snapshot()))
    monkeypatch.setattr('custom_components.unraid_ssh.coordinator.UpdateCoordinator._async_update_data', AsyncMock(return_value=UpdateState(
        {'web': ImageStatus('web', 'example/web:latest', 'sha256:old', 'sha256:new', True)}, None)))
    entry = MockConfigEntry(domain='unraid_ssh', title='Example Unraid', data={'host': 'example.invalid'})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def states_for_entry(hass, entry):
    registry = er.async_get(hass)
    return [hass.states.get(entity_id) if (entity_id := registry.async_get_entity_id(domain, 'unraid_ssh', entry.entry_id + suffix)) else None
            for domain, suffix in [('switch', '_container_web'), ('update', '_update_web')]]


@pytest.mark.parametrize('existing_local', [False, True])
async def test_first_install_http_pictures_two_entries_reload_remove(hass, hass_client_no_auth, tmp_path, monkeypatch, existing_local):
    hass.config.config_dir = str(tmp_path)
    assert await async_setup_component(hass, 'http', {})
    # Same optional /local registration used by frontend when www preexists.
    if existing_local:
        (tmp_path / 'www').mkdir()
        (tmp_path / 'www/other.txt').write_text('unrelated')
        await hass.http.async_register_static_paths([StaticPathConfig('/local', str(tmp_path / 'www'), True)])
    client = PictureSSH()
    entry = await setup_entry(hass, monkeypatch, client)
    entities = states_for_entry(hass, entry)
    assert all(entity is not None for entity in entities)
    assert all('entity_picture' in entity.attributes for entity in entities)
    picture = entities[0].attributes['entity_picture']
    assert entities[1].attributes['entity_picture'] == picture
    assert client.calls == 1
    http = await hass_client_no_auth()
    response = await http.get(picture)
    assert response.status == 200 and await response.read() == PNG
    if existing_local:
        assert await (await http.get('/local/other.txt')).text() == 'unrelated'
    second = await setup_entry(hass, monkeypatch, PictureSSH())
    assert states_for_entry(hass, second)[0].attributes['entity_picture'] != picture
    client.data = SVG
    entry.runtime_data.coordinator.async_set_updated_data(snapshot(revision='2:100'))
    await hass.async_block_till_done()
    changed = states_for_entry(hass, entry)
    assert changed[0].attributes['entity_picture'] == changed[1].attributes['entity_picture']
    assert changed[0].attributes['entity_picture'] != picture
    # Transport is external; platforms, registry and entry unload/reload are real.
    old_cache = entry.runtime_data.icons
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = states_for_entry(hass, entry)[0].attributes['entity_picture']
    assert (await http.get(reloaded)).status == 200
    assert old_cache._closed and not old_cache._listeners and not old_cache._workers
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not (tmp_path / 'www/unraid_ssh' / entry.entry_id).exists()
    second_picture = states_for_entry(hass, second)[0].attributes['entity_picture']
    assert (await http.get(second_picture)).status == 200
    assert (await http.get('/local/unraid_ssh/../.storage/core.config_entries')).status == 404
    await hass.config_entries.async_unload(second.entry_id)


async def test_entry_unload_cancels_running_picture_job(hass, tmp_path, monkeypatch):
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH()
    entry = await setup_entry(hass, monkeypatch, client)
    client.started.clear()
    client.release.clear()
    entry.runtime_data.coordinator.async_set_updated_data(snapshot(revision='2:100'))
    await client.started.wait()
    cache = entry.runtime_data.icons
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert client.active == 0 and not cache._workers and not cache._listeners


async def test_slow_only_container_is_added_with_picture_when_fast_poll_finds_it(hass, tmp_path, monkeypatch):
    hass.config.config_dir = str(tmp_path)
    client = PictureSSH()
    entry = await setup_entry(hass, monkeypatch, client, snapshot(names=()))
    assert states_for_entry(hass, entry) == [None, None]
    entry.runtime_data.coordinator.async_set_updated_data(snapshot())
    await hass.async_block_till_done()
    switch, update = states_for_entry(hass, entry)
    assert switch.attributes['entity_picture'] == update.attributes['entity_picture']
    assert switch.attributes['entity_picture'].startswith('/local/unraid_ssh/')
    ids = [switch.entity_id, update.entity_id]
    entry.runtime_data.coordinator.async_set_updated_data(snapshot())
    await hass.async_block_till_done()
    assert [state.entity_id for state in states_for_entry(hass, entry)] == ids
    assert client.calls == 1
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('failure', ['symlink', 'permission'])
@pytest.mark.parametrize('url', ['', 'https://example.com/web.png'])
async def test_unusable_www_keeps_entities_loaded_with_safe_fallback(hass, hass_client_no_auth, tmp_path, monkeypatch, failure, url, caplog):
    from pathlib import Path
    import logging

    hass.config.config_dir = str(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    marker = outside / 'keep.txt'
    marker.write_text('untouched')
    if failure == 'symlink':
        (tmp_path / 'www').symlink_to(outside, target_is_directory=True)
    else:
        original_mkdir = Path.mkdir

        def mkdir(path, *args, **kwargs):
            if path == tmp_path / 'www/unraid_ssh':
                raise PermissionError('synthetic cache permission denial')
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'mkdir', mkdir)
    client = PictureSSH()
    entry = await setup_entry(hass, monkeypatch, client, snapshot(url=url))
    switch, update = states_for_entry(hass, entry)
    assert switch.state == 'on' and update.state == 'on'
    assert switch.attributes.get('entity_picture') == update.attributes.get('entity_picture') == (url or None)
    assert client.calls == 0
    http = await hass_client_no_auth()
    assert (await http.get('/local/unraid_ssh/example.png')).status == 404
    assert list(outside.iterdir()) == [marker]
    assert marker.read_text() == 'untouched'
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert marker.read_text() == 'untouched'
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
