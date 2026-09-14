"""Acceptance of a fresh layout through normal integration/platform setup."""
from dataclasses import replace

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from test_device_migration import inventory, load, lookup, own_entities


@pytest.mark.parametrize('language,loose_word,disk_word', [
    ('de', 'Freie Container', 'Festplatte'),
    ('en', 'Standalone containers', 'Disk'),
])
async def test_fresh_layout_and_reload(hass, monkeypatch, tmp_path, language, loose_word, disk_word):
    hass.config.config_dir = str(tmp_path)
    hass.config.language = language
    entry = MockConfigEntry(domain='unraid_ssh', title='Example Unraid', data={'host': 'example.invalid'})
    entry.add_to_hass(hass)
    await load(hass, monkeypatch, entry, inventory())
    devices, entities = dr.async_get(hass), er.async_get(hass)
    own = own_entities(hass, entry)
    assert len(dr.async_entries_for_config_entry(devices, entry.entry_id)) == 7
    assert lookup(hass, entry, '_containers').name == f'Example Unraid {loose_word}'
    assert lookup(hass, entry, '_gpu_0') is None
    assert lookup(hass, entry, '_disk_disk1').name == f'Example Unraid {disk_word} disk1'
    assert lookup(hass, entry, '_vm_Guest').name == 'Example Unraid VM Guest'
    assert own['_container_alpha_one'].device_id == own['_container_beta'].device_id
    server = lookup(hass, entry, '')
    for name in ('Media Backup', 'Media_Backup'):
        share = lookup(hass, entry, '_share_' + name)
        for metric in ('used', 'free'):
            entity = own[f'_share_{metric}_{name}']
            assert entity.device_id == share.id != server.id
            assert hass.states.get(entity.entity_id).attributes['friendly_name'].count(name) == 1
    first = own['_container_alpha_one']
    entities.async_update_entity(first.entity_id, name='Personal switch')
    devices.async_update_device(server.id, name_by_user='Personal server')
    before = {e.entity_id: (e.unique_id, e.device_id, e.name) for e in own_entities(hass, entry).values()}
    ids = {d.id for d in dr.async_entries_for_config_entry(devices, entry.entry_id)}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert before == {e.entity_id: (e.unique_id, e.device_id, e.name) for e in own_entities(hass, entry).values()}
    assert ids == {d.id for d in dr.async_entries_for_config_entry(devices, entry.entry_id)}
    assert lookup(hass, entry, '').name_by_user == 'Personal server'
    await hass.config_entries.async_unload(entry.entry_id)


async def test_empty_inventory_does_not_create_container_device(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain='unraid_ssh', title='Example Unraid', data={'host': 'example.invalid'})
    entry.add_to_hass(hass)
    await load(hass, monkeypatch, entry, replace(inventory(), containers=(), template_containers=()))
    assert lookup(hass, entry, '_containers') is None
    await hass.config_entries.async_unload(entry.entry_id)
