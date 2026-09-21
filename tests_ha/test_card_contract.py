"""Card actions and metadata through real Home Assistant platform setup."""
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import async_get_platforms
from custom_components.unraid_ssh.model import ImageStatus, Stack
from custom_components.unraid_ssh.parse import Container
from custom_components.unraid_ssh.const import STALE_GRACE
from custom_components.unraid_ssh.ssh import CommandResult
from test_device_migration import inventory, legacy, load, own_entities, lookup


def compose_snapshot(name='old-web', replica=1):
    c = Container(name, 'running', 'example/web', 'project', 'web', replica=replica)
    stack = Stack('project', 'project', '/stacks/Stable', False, True, 1, ('/stacks/Stable/compose.yaml',), (c,))
    return replace(inventory(), containers=(c,), template_containers=(), stacks=(stack,))


def loaded(hass, role, kind='container'):
    return [e for p in async_get_platforms(hass, 'unraid_ssh') for e in p.entities.values()
            if getattr(e, 'extra_state_attributes', None)
            and e.extra_state_attributes.get('role') == role and e.extra_state_attributes.get('kind') == kind]


async def test_standalone_actions_share_metadata_but_not_devices(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, inventory(), {'alpha_one': ImageStatus('alpha_one','example/a','sha256:a','sha256:b',True)})
    controls = loaded(hass, 'control')
    assert len(controls) == 2
    assert len({er.async_get(hass).async_get(e.entity_id).device_id for e in controls}) == 2
    for e in controls:
        d = dr.async_get(hass).async_get(er.async_get(hass).async_get(e.entity_id).device_id)
        assert d.model == 'Docker container'
    control = next(e for e in controls if e.extra_state_attributes['container_name'] == 'alpha_one')
    update = loaded(hass, 'update')[0]
    restart = next(e for e in loaded(hass, 'restart') if e.extra_state_attributes['container_key'] == control.extra_state_attributes['container_key'])
    assert update.extra_state_attributes['container_key'] == control.extra_state_attributes['container_key']
    assert len({er.async_get(hass).async_get(e.entity_id).device_id for e in (control, update, restart)}) == 1
    fast = entry.runtime_data.coordinator
    fast.client.run = AsyncMock(return_value=CommandResult(0, '', ''))
    fast.async_request_refresh = AsyncMock()
    await restart.async_press()
    fast.client.run.assert_awaited_once_with('docker restart alpha_one', timeout=60)
    fast.async_request_refresh.assert_awaited_once()
    await hass.config_entries.async_unload(entry.entry_id)


async def test_compose_migration_preserves_entity_id_and_recreation_uses_current_name(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, old = legacy(hass)
    dev = old.device('_stack_Stable')
    existing = old.entity('switch','_container_old-web',dev)
    await load(hass, monkeypatch, entry, compose_snapshot())
    control = loaded(hass, 'control')[0]
    assert control.entity_id == existing.entity_id
    identity = er.async_get(hass).async_get(existing.entity_id).unique_id
    assert identity != existing.unique_id
    restart = loaded(hass, 'restart')[0]
    fast = entry.runtime_data.coordinator
    fast.async_set_updated_data(compose_snapshot('replacement-web'))
    await hass.async_block_till_done()
    assert loaded(hass, 'control') == [control]
    assert er.async_get(hass).async_get(existing.entity_id).unique_id == identity
    assert control.extra_state_attributes['container_name'] == 'replacement-web'
    fast.client.run = AsyncMock(return_value=CommandResult(0, '', ''))
    fast.async_request_refresh = AsyncMock()
    await restart.async_press()
    fast.client.run.assert_awaited_once_with('docker restart replacement-web', timeout=60)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_native_stack_restart_refreshes_on_error_and_has_no_fake_fallback(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, compose_snapshot())
    restart = loaded(hass, 'restart', 'stack')[0]
    fast = entry.runtime_data.coordinator
    fast.client.run = AsyncMock(return_value=CommandResult(1, '', 'failed'))
    fast.async_request_refresh = AsyncMock()
    with pytest.raises(HomeAssistantError):
        await restart.async_press()
    assert ' restart' in fast.client.run.call_args.args[0]
    fast.async_request_refresh.assert_awaited_once()
    fast.async_set_updated_data(replace(fast.data, stacks=(replace(fast.data.stacks[0], config_files=()),)))
    await hass.async_block_till_done()
    assert not restart.available
    with pytest.raises(HomeAssistantError):
        await restart.async_press()
    assert fast.client.run.await_count == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_old_entities_and_devices_are_not_deleted_during_identity_transition(hass, monkeypatch, tmp_path, freezer):
    hass.config.config_dir = str(tmp_path)
    entry, _, old = legacy(hass)
    before = own_entities(hass, entry)
    await load(hass, monkeypatch, entry, inventory())
    empty = replace(inventory(), containers=(), template_containers=())
    entry.runtime_data.coordinator.async_set_updated_data(empty)
    freezer.tick(STALE_GRACE * 3)
    entry.runtime_data.coordinator.async_set_updated_data(empty)
    await hass.async_block_till_done()
    for suffix in ('_container_alpha_one', '_update_alpha_one', '_container_beta', '_update_beta'):
        assert own_entities(hass, entry)[suffix].entity_id == before[suffix].entity_id
    assert dr.async_get(hass).async_get(old.alpha.id) is not None
    await hass.config_entries.async_unload(entry.entry_id)


async def test_metadata_recovery_does_not_duplicate_loaded_entities(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, compose_snapshot(replica=None))
    before = loaded(hass, 'control')[0].entity_id
    entry.runtime_data.coordinator.async_set_updated_data(compose_snapshot())
    await hass.async_block_till_done()
    controls = loaded(hass, 'control')
    assert len(controls) == 1
    assert controls[0].entity_id == before
    assert controls[0].available
    entry.runtime_data.coordinator.async_set_updated_data(compose_snapshot('recreated'))
    await hass.async_block_till_done()
    assert len(loaded(hass, 'control')) == 1
    assert loaded(hass, 'control')[0].extra_state_attributes['container_name'] == 'recreated'
    await hass.config_entries.async_unload(entry.entry_id)


async def test_existing_canonical_entity_wins_without_deleting_legacy_alias(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, old = legacy(hass)
    dev = old.device('_stack_Stable')
    alias = old.entity('switch','_container_old-web',dev)
    canonical = old.entity('switch','_container_compose:["Stable","web",1]',dev)
    await load(hass, monkeypatch, entry, compose_snapshot())
    assert loaded(hass, 'control')[0].entity_id == canonical.entity_id
    assert er.async_get(hass).async_get(alias.entity_id).unique_id == alias.unique_id
    await hass.config_entries.async_unload(entry.entry_id)


async def test_ambiguous_compose_labels_create_no_new_hash_entities(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, compose_snapshot())
    before = {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    snap = compose_snapshot()
    duplicate = replace(snap.containers[0], name='transient-hash')
    duplicated = replace(snap, containers=(*snap.containers, duplicate), stacks=(replace(snap.stacks[0], containers=(*snap.containers, duplicate)),))
    entry.runtime_data.coordinator.async_set_updated_data(duplicated)
    await hass.async_block_till_done()
    after = {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    assert after == before
    assert not loaded(hass, 'control')[0].available
    entry.runtime_data.coordinator.async_set_updated_data(compose_snapshot('replacement-web'))
    await hass.async_block_till_done()
    assert loaded(hass, 'control')[0].available
    await hass.config_entries.async_unload(entry.entry_id)


async def test_update_follows_recreated_container_and_install_uses_current_service(hass, monkeypatch, tmp_path):
    from custom_components.unraid_ssh.coordinator import UpdateState
    hass.config.config_dir = str(tmp_path)
    entry, _, old = legacy(hass)
    dev = old.device('_stack_Stable')
    before = old.entity('update', '_update_old-web', dev)
    await load(hass, monkeypatch, entry, compose_snapshot(), {'old-web': ImageStatus('old-web','example/web','sha256:old','sha256:new',True)})
    update = loaded(hass, 'update')[0]
    assert update.entity_id == before.entity_id
    fast, slow = entry.runtime_data.coordinator, entry.runtime_data.updates
    fast.async_set_updated_data(compose_snapshot('replacement-web'))
    slow.async_set_updated_data(UpdateState({'replacement-web': ImageStatus('replacement-web','example/new','sha256:local','sha256:remote',True)}, None))
    await hass.async_block_till_done()
    assert len(loaded(hass, 'update')) == 1
    assert update.title == 'example/new' and update.installed_version == 'local'
    fast.client.run = AsyncMock(return_value=CommandResult(0, '', ''))
    fast.async_request_refresh = AsyncMock()
    slow.async_request_refresh = AsyncMock()
    await update.async_install(None, False)
    command = fast.client.run.call_args.args[0]
    assert ' pull web' in command and ' up -d web' in command
    fast.async_request_refresh.assert_awaited_once()
    slow.async_request_refresh.assert_awaited_once()
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('missing', ['replica', 'service', 'project'])
async def test_known_compose_container_label_loss_never_creates_aliases(hass, monkeypatch, tmp_path, missing):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, compose_snapshot(), {'old-web': ImageStatus('old-web','example/web','sha256:old','sha256:new',True)})
    before = {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}
    control = loaded(hass, 'control')[0]
    snap = compose_snapshot()
    c = replace(snap.containers[0], **{missing: None if missing == 'replica' else ''})
    incomplete = replace(snap, containers=(c,), stacks=(replace(snap.stacks[0], containers=(c,)),))
    entry.runtime_data.coordinator.async_set_updated_data(incomplete)
    await hass.async_block_till_done()
    assert {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)} == before
    assert len(loaded(hass, 'control')) == 1
    assert not control.available
    entry.runtime_data.coordinator.async_set_updated_data(compose_snapshot())
    await hass.async_block_till_done()
    assert loaded(hass, 'control') == [control]
    assert control.available
    assert {e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)} == before
    await hass.config_entries.async_unload(entry.entry_id)


async def test_compose_label_loss_protection_survives_reload(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    await load(hass, monkeypatch, entry, compose_snapshot())
    registry = er.async_get(hass)
    before = {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    control_id = loaded(hass, 'control')[0].entity_id
    monkeypatch.setattr('custom_components.unraid_ssh.coordinator.UnraidCoordinator._async_update_data', AsyncMock(return_value=compose_snapshot(replica=None)))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert {e.entity_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)} == before
    entry.runtime_data.coordinator.async_set_updated_data(compose_snapshot())
    await hass.async_block_till_done()
    assert [e.entity_id for e in loaded(hass, 'control')] == [control_id]
    assert loaded(hass, 'control')[0].available
    await hass.config_entries.async_unload(entry.entry_id)
