"""Migration against real HA registries, with synthetic legacy installations."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.unraid_ssh.model import Stack
from custom_components.unraid_ssh.parse import Container, Disk, Gpu, Share, Vm
from test_device_naming import _snapshot
from test_icon_cache import PictureSSH

DOMAIN = 'unraid_ssh'


def inventory():
    # Template containers: Unraid's Docker manager labels what it owns.
    containers = (Container('alpha_one', 'running', 'example/a', '', '', managed='dockerman'),
                  Container('beta', 'exited', 'example/b', '', '', managed='dockerman'))
    return replace(_snapshot(containers=containers, loose=containers,
        shares=(Share('Media Backup', 10, 20), Share('Media_Backup', 30, 40)),
        gpus=(Gpu(0, 'Example GPU', 10, 20, 100, 20, 40, 12),)),
        disks=(Disk('disk1', 'sdb', 'Data', 'ok', 30, False, 100, 50, 50, 0, 'Mounted'),),
        vms=(Vm('Guest', 'running'),),
        stacks=(Stack('project', 'project', '/stacks/My Stack', False, True, 0, (), ()),))


def legacy(hass):
    devices, entities = dr.async_get(hass), er.async_get(hass)
    entry = MockConfigEntry(domain=DOMAIN, title='Example Unraid', data={'host': 'example.invalid'})
    entry.add_to_hass(hass)
    other = MockConfigEntry(domain=DOMAIN, title='Other Unraid', data={'host': 'other.invalid'}, disabled_by=ConfigEntryDisabler.USER)
    other.add_to_hass(hass)

    def device(suffix, owner=entry):
        return devices.async_get_or_create(config_entry_id=owner.entry_id,
            identifiers={(DOMAIN, owner.entry_id + suffix)}, name='Old device')

    def entity(domain, suffix, dev, *, owner=entry, **kwargs):
        return entities.async_get_or_create(domain, DOMAIN, owner.entry_id + suffix,
            config_entry=owner, device_id=dev.id, suggested_object_id='old_' + suffix.strip('_'), **kwargs)

    server = device('')
    alpha, beta, gpu = device('_container_alpha_one'), device('_container_beta'), device('_gpu_0')
    for domain, suffix, dev in [('switch', '_container_alpha_one', alpha), ('update', '_update_alpha_one', alpha),
            ('switch', '_container_beta', beta), ('update', '_update_beta', beta),
            ('sensor', '_gpu_temp_0', gpu), ('sensor', '_gpu_util_0', gpu),
            ('sensor', '_gpu_vram_0', gpu), ('sensor', '_gpu_power_0', gpu)]:
        entity(domain, suffix, dev, disabled_by=er.RegistryEntryDisabler.USER if suffix == '_update_beta' else None)
    for name in ('Media Backup', 'Media_Backup'):
        entity('sensor', '_share_used_' + name, server)
        entity('sensor', '_share_free_' + name, server, disabled_by=er.RegistryEntryDisabler.USER)
    for domain, suffix in [('switch', '_vm_Guest'), ('sensor', '_disk_temp_disk1'), ('switch', '_stack_My Stack')]:
        device_suffix = '_disk_disk1' if domain == 'sensor' else suffix
        dev = device(device_suffix)
        entity(domain, suffix, dev, disabled_by=er.RegistryEntryDisabler.USER)
    second_device = device('_container_alpha_one', other)
    second_entity = entity('switch', '_container_alpha_one', second_device, owner=other)
    return entry, other, SimpleNamespace(device=device, entity=entity, server=server, alpha=alpha,
        beta=beta, gpu=gpu, second_device=second_device, second_entity=second_entity)


def lookup(hass, entry, suffix):
    return next((device for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
                 if (DOMAIN, entry.entry_id + suffix) in device.identifiers
                 and entry.entry_id in device.config_entries), None)


def own_entities(hass, entry):
    return {e.unique_id.removeprefix(entry.entry_id): e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


async def load(hass, monkeypatch, entry, snapshot, images=None):
    from custom_components.unraid_ssh.coordinator import UpdateState
    monkeypatch.setattr('custom_components.unraid_ssh.build_client', lambda entry: PictureSSH())
    monkeypatch.setattr('custom_components.unraid_ssh.coordinator.UnraidCoordinator._async_update_data', AsyncMock(return_value=snapshot))
    monkeypatch.setattr('custom_components.unraid_ssh.coordinator.UpdateCoordinator._async_update_data', AsyncMock(return_value=UpdateState(images or {}, None)))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize('language, disk_word, containers_word', [('de', 'Festplatte', 'Freie Container'), ('en', 'Disk', 'Standalone containers')])
async def test_setup_migrates_legacy_settings_and_translated_names(hass, monkeypatch, tmp_path, language, disk_word, containers_word, caplog):
    hass.config.config_dir = str(tmp_path)
    hass.config.language = language
    entry, other, old = legacy(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    from homeassistant.helpers import area_registry as ar, label_registry as lr
    area = ar.async_get(hass).async_create('Old room')
    explicit = ar.async_get(hass).async_create('Explicit room')
    label = lr.async_get(hass).async_create('Old label')
    own_label = lr.async_get(hass).async_create('Own label')
    devices.async_update_device(old.alpha.id, area_id=area.id, labels={label.label_id}, name_by_user='My container')
    original = own_entities(hass, entry)
    entities.async_update_entity(original['_container_alpha_one'].entity_id, name='My switch', area_id=explicit.id, labels={own_label.label_id})
    devices.async_update_device(lookup(hass, entry, '_vm_Guest').id, name_by_user='My VM')
    stable_devices = {suffix: lookup(hass, entry, suffix).id for suffix in ('', '_vm_Guest', '_disk_disk1', '_stack_My Stack')}
    before = {e.entity_id: (e.unique_id, e.name, e.disabled_by) for e in own_entities(hass, entry).values()}
    second_before = old.second_entity
    await load(hass, monkeypatch, entry, inventory())
    current = own_entities(hass, entry)
    for e in current.values():
        if e.entity_id in before:
            assert (e.unique_id, e.name, e.disabled_by) == before[e.entity_id]
    assert set(before) <= {e.entity_id for e in current.values()}
    assert lookup(hass, entry, '_container_alpha_one').model == 'Docker container'
    for suffix in ('_container_alpha_one', '_update_alpha_one'):
        assert current[suffix].device_id == old.alpha.id
    for suffix in ('_container_beta', '_update_beta'):
        assert current[suffix].device_id == old.beta.id
    assert current['_container_alpha_one'].area_id == explicit.id
    assert current['_container_alpha_one'].labels | devices.async_get(old.alpha.id).labels == {own_label.label_id, label.label_id}
    assert current['_update_alpha_one'].area_id is None
    assert devices.async_get(old.alpha.id).area_id == area.id
    assert devices.async_get(old.alpha.id).labels == {label.label_id}
    for suffix in ('_gpu_temp_0', '_gpu_util_0', '_gpu_vram_0', '_gpu_power_0'):
        assert current[suffix].device_id == old.server.id
    for name in ('Media Backup', 'Media_Backup'):
        share = lookup(hass, entry, '_share_' + name)
        assert share.name == 'Example Unraid Share ' + name
        assert current['_share_used_' + name].device_id == share.id
        assert current['_share_free_' + name].device_id == share.id
    for suffix, name in [('_vm_Guest', 'Example Unraid VM Guest'), ('_disk_disk1', f'Example Unraid {disk_word} disk1'), ('_stack_My Stack', 'Example Unraid Stack My Stack')]:
        assert lookup(hass, entry, suffix).name == name
    assert lookup(hass, entry, '_vm_Guest').name_by_user == 'My VM'
    for suffix, device_id in stable_devices.items():
        assert lookup(hass, entry, suffix).id == device_id
    assert 'via_device' not in caplog.text
    assert devices.async_get(old.alpha.id) is not None and devices.async_get(old.beta.id) is not None
    assert devices.async_get(old.gpu.id) is None and devices.async_get(old.server.id) is not None
    assert entities.async_get(second_before.entity_id) == second_before
    from custom_components.unraid_ssh.migration import async_reconcile_devices
    with patch.object(devices, 'async_get_or_create', wraps=devices.async_get_or_create) as create, patch.object(entities, 'async_update_entity', wraps=entities.async_update_entity) as update:
        async_reconcile_devices(hass, entry)
        assert create.call_count == update.call_count == 0
    device_ids = {device.id for device in dr.async_entries_for_config_entry(devices, entry.entry_id)}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert {device.id for device in dr.async_entries_for_config_entry(devices, entry.entry_id)} == device_ids
    for entity_id, identity in before.items():
        item = entities.async_get(entity_id)
        assert (item.unique_id, item.name, item.disabled_by) == identity
    await hass.config_entries.async_unload(entry.entry_id)


def stack_inventory(failed=None):
    """Real merging loses the folder/project match for either failed section."""
    from custom_components.unraid_ssh.model import merge_stacks
    from custom_components.unraid_ssh.parse import ComposeProject, StackDir
    base = inventory()
    member = replace(base.containers[0], project='project', service='web')
    containers = (member, base.containers[1])
    stacks, loose = merge_stacks(
        [] if failed == 'stacks' else [StackDir('/stacks/My Stack', 'My Stack', False)],
        [] if failed == 'compose' else [ComposeProject('project', 'running(1)', 1, ('/stacks/My Stack/compose.yaml',))],
        list(containers),
    )
    return replace(base, containers=containers, stacks=stacks, template_containers=loose,
                   failed=frozenset({failed}) if failed else frozenset())


@pytest.mark.parametrize('failed', ['compose', 'stacks'])
@pytest.mark.parametrize('previous_suffix', ['_stack_My Stack', '_container_alpha_one'])
async def test_partial_stack_setup_preserves_existing_assignments_and_recovery(
        hass, monkeypatch, tmp_path, failed, previous_suffix):
    from custom_components.unraid_ssh.model import ImageStatus
    from homeassistant.helpers.entity_platform import async_get_platforms
    hass.config.config_dir = str(tmp_path)
    entry, _, old = legacy(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    previous = lookup(hass, entry, previous_suffix)
    before = own_entities(hass, entry)
    tracked = ('_container_alpha_one', '_update_alpha_one')
    for suffix in tracked:
        entities.async_update_entity(before[suffix].entity_id, device_id=previous.id)
    before = {suffix: entities.async_get(before[suffix].entity_id) for suffix in tracked}
    images = {'alpha_one': ImageStatus('alpha_one', 'example/a', 'sha256:old', 'sha256:new', True)}
    await load(hass, monkeypatch, entry, stack_inventory(failed), images)
    loaded = [entity for platform in async_get_platforms(hass, DOMAIN)
              for entity in platform.entities.values()
              if entity.entity_id in {item.entity_id for item in before.values()}]
    assert len(loaded) == 2
    for suffix, item in before.items():
        current = own_entities(hass, entry)[suffix]
        assert (current.entity_id, current.unique_id, current.device_id) == (item.entity_id, item.unique_id, previous.id)
    assert all(entity.device_info['identifiers'] == previous.identifiers for entity in loaded)
    assert lookup(hass, entry, '_stack_project') is None
    # Successful Docker labels suffice for a free container even during stack failure.
    assert own_entities(hass, entry)['_container_beta'].device_id == lookup(hass, entry, '_container_beta').id
    fast = entry.runtime_data.coordinator
    fast.async_set_updated_data(stack_inventory())
    await hass.async_block_till_done()
    stable = lookup(hass, entry, '_stack_My Stack')
    assert all(own_entities(hass, entry)[suffix].device_id == stable.id for suffix in tracked)
    assert all(entity.device_info['identifiers'] == stable.identifiers for entity in loaded)
    # A subsequent partial update also leaves loaded instances and IDs in place.
    fast.async_set_updated_data(stack_inventory(failed))
    await hass.async_block_till_done()
    assert all(own_entities(hass, entry)[suffix].device_id == stable.id for suffix in tracked)
    assert all(entity.device_info['identifiers'] == stable.identifiers for entity in loaded)
    assert lookup(hass, entry, '_stack_project') is None
    fast.async_set_updated_data(stack_inventory())
    await hass.async_block_till_done()
    assert lookup(hass, entry, '_stack_My Stack').id == stable.id
    for suffix, item in before.items():
        current = own_entities(hass, entry)[suffix]
        assert (current.entity_id, current.unique_id) == (item.entity_id, item.unique_id)
    assert devices.async_get(old.server.id) is not None
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('failed', ['compose', 'stacks'])
async def test_partial_stack_discovery_defers_new_entities_until_recovery(hass, monkeypatch, tmp_path, failed):
    from custom_components.unraid_ssh.model import ImageStatus
    hass.config.config_dir = str(tmp_path)
    entry = MockConfigEntry(domain=DOMAIN, title='Example Unraid', data={'host': 'example.invalid'})
    entry.add_to_hass(hass)
    images = {name: ImageStatus(name, 'example/image', 'sha256:old', 'sha256:new', True)
              for name in ('alpha_one', 'beta')}
    await load(hass, monkeypatch, entry, stack_inventory(failed), images)
    current = own_entities(hass, entry)
    assert '_container_alpha_one' not in current and '_update_alpha_one' not in current
    assert lookup(hass, entry, '_stack_project') is None
    assert current['_container_beta'].device_id == current['_update_beta'].device_id == lookup(hass, entry, '_container_beta').id
    entry.runtime_data.coordinator.async_set_updated_data(stack_inventory())
    await hass.async_block_till_done()
    current = own_entities(hass, entry)
    assert current['_container_alpha_one'].device_id == current['_update_alpha_one'].device_id == lookup(hass, entry, '_stack_My Stack').id
    assert hass.states.get(current['_container_alpha_one'].entity_id) is not None
    assert hass.states.get(current['_update_alpha_one'].entity_id) is not None
    assert lookup(hass, entry, '_stack_project') is None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_absent_owned_container_disable_and_cleanup_guards(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, other, old = legacy(hass)
    devices, entities = dr.async_get(hass), er.async_get(hass)
    shared_device = None
    for name in ('gone_name', 'foreign', 'unknown', 'shared', 'extra'):
        dev = old.device('_container_' + name)
        old.entity('switch', '_container_' + name, dev, disabled_by=er.RegistryEntryDisabler.DEVICE)
        old.entity('update', '_update_' + name, dev, disabled_by=er.RegistryEntryDisabler.USER)
        devices.async_update_device(dev.id, disabled_by=dr.DeviceEntryDisabler.USER)
        if name == 'foreign':
            entities.async_get_or_create('sensor', 'another_domain', 'foreign', config_entry=other, device_id=dev.id)
        if name == 'unknown':
            old.entity('sensor', '_unknown_metric', dev)
        if name == 'shared':
            shared_device = devices.async_get_or_create(config_entry_id=other.entry_id, identifiers=dev.identifiers)
        if name == 'extra':
            devices.async_update_device(dev.id, new_identifiers=dev.identifiers | {('another_domain', 'extra')})
    await load(hass, monkeypatch, entry, inventory())
    current = own_entities(hass, entry)
    for name in ('gone_name', 'foreign', 'unknown', 'shared', 'extra'):
        device = lookup(hass, entry, '_container_' + name)
        assert device is not None
        assert current['_container_' + name].device_id == device.id
        assert current['_container_' + name].disabled_by == er.RegistryEntryDisabler.DEVICE
        assert hass.states.get(current['_container_' + name].entity_id) is None
    # HA 2026.7 has shared devices; 2026.9 keeps one device per owner.
    assert devices.async_get(shared_device.id) is not None
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('failed, suffix, old_device', [('docker', '_container_alpha_one', 'alpha'), ('gpu', '_gpu_temp_0', 'gpu'), ('shares', '_share_used_Media Backup', 'server')])
async def test_failed_sections_do_not_migrate_then_recover(hass, monkeypatch, tmp_path, failed, suffix, old_device):
    hass.config.config_dir = str(tmp_path)
    entry, _, old = legacy(hass)
    absent = old.device('_container_absent')
    old.entity('update', '_update_absent', absent, disabled_by=er.RegistryEntryDisabler.USER)
    # The failed section may carry stale/partial data: migration must check failed.
    await load(hass, monkeypatch, entry, replace(inventory(), failed=frozenset({failed})))
    assert own_entities(hass, entry)[suffix].device_id == getattr(old, old_device).id
    if failed == 'docker':
        assert lookup(hass, entry, '_container_absent') is not None
        assert own_entities(hass, entry)['_update_absent'].device_id == absent.id
    entry.runtime_data.coordinator.async_set_updated_data(inventory())
    await hass.async_block_till_done()
    assert (own_entities(hass, entry)[suffix].device_id == getattr(old, old_device).id) is (failed == 'docker')
    await hass.config_entries.async_unload(entry.entry_id)


async def test_loaded_switch_and_update_follow_stack_changes_and_new_share(hass, monkeypatch, tmp_path):
    from test_container_pictures import setup_entry, states_for_entry
    from test_icon_cache import snapshot
    hass.config.config_dir = str(tmp_path)
    hass.config.language = 'en'
    entry = await setup_entry(hass, monkeypatch, PictureSSH(), snapshot())
    before = states_for_entry(hass, entry)
    assert all(s.attributes['friendly_name'].startswith('Example Unraid web') for s in before)
    fast = entry.runtime_data.coordinator
    member = replace(fast.data.containers[0], project='project', service='web')
    stack = Stack('project', 'project', '/stacks/My Stack', False, True, 1, (), (member,))
    moved = replace(fast.data, containers=(member,), template_containers=(), stacks=(stack,), shares=(Share('New Share', 10, 20),))
    fast.async_set_updated_data(moved)
    await hass.async_block_till_done()
    after = states_for_entry(hass, entry)
    assert [s.entity_id for s in after] == [s.entity_id for s in before]
    assert all('Stack My Stack' in s.attributes['friendly_name'] for s in after)
    from homeassistant.helpers.entity_platform import async_get_platforms
    loaded = [entity for platform in async_get_platforms(hass, DOMAIN)
              for entity in platform.entities.values()
              if entity.entity_id in {state.entity_id for state in after}]
    assert len(loaded) == 2
    assert all(entity.device_info['identifiers'] == {(DOMAIN, entry.entry_id + '_stack_My Stack')}
               for entity in loaded)
    assert lookup(hass, entry, '_share_New Share') is not None
    from homeassistant.helpers.entity_component import async_update_entity
    # State update exercises the loaded instances, with no platform reload.
    for state in after:
        await async_update_entity(hass, state.entity_id)
    fast.async_set_updated_data(snapshot())
    await hass.async_block_till_done()
    assert all(s.attributes['friendly_name'].startswith('Example Unraid web') for s in states_for_entry(hass, entry))
    assert all(entity.device_info['identifiers'] == {(DOMAIN, entry.entry_id + '_container_web')}
               for entity in loaded)
    await hass.config_entries.async_unload(entry.entry_id)
