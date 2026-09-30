"""Real platform contract for resource identity, missing stats and reboot."""
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.exceptions import HomeAssistantError
from custom_components.unraid_ssh import parse
from custom_components.unraid_ssh.const import STALE_GRACE
from custom_components.unraid_ssh.ssh import CommandResult
from test_device_migration import inventory, legacy, load
from test_card_contract import loaded, compose_snapshot


async def test_resource_metadata_units_and_stats_outage_preserves_identity(hass, monkeypatch, tmp_path, freezer):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    snap = replace(inventory(), container_resources={'alpha_one': parse.ResourceSample(150, 1024, 4096)},
                   vm_resources={'Guest': parse.VmResourceSample(1, 120, 8192, 16384, 9999, None)})
    await load(hass, monkeypatch, entry, snap)
    cpu = loaded(hass, 'cpu')[0]
    ram = loaded(hass, 'ram_used')[0]
    vm_ram = loaded(hass, 'ram_allocated', 'vm')[0]
    vm_used = loaded(hass, 'ram_used', 'vm')[0]
    assert cpu.native_value == 150 and cpu.native_unit_of_measurement == '%'
    assert ram.native_value == 1024 and ram.native_unit_of_measurement == 'B'
    assert vm_ram.native_value == 8192 and vm_used.native_value is None
    assert not vm_used.available
    state = loaded(hass, 'state', 'vm')[0]
    assert state.extra_state_attributes['vm_key'] == 'Guest'
    entities = er.async_get(hass)
    def identity(e):
        item = entities.async_get(e.entity_id)
        return item.entity_id, item.unique_id, item.device_id
    before = [identity(e) for e in (cpu, ram, vm_ram, state)]
    entry.runtime_data.coordinator.async_set_updated_data(replace(snap, container_resources={}, vm_resources={}, failed=frozenset({'docker_stats','vm_stats'})))
    await hass.async_block_till_done()
    freezer.tick(STALE_GRACE * 3)
    entry.runtime_data.coordinator.async_set_updated_data(replace(snap, container_resources={}, vm_resources={}, failed=frozenset({'docker_stats','vm_stats'})))
    await hass.async_block_till_done()
    assert [identity(e) for e in (cpu, ram, vm_ram, state)] == before
    assert not cpu.available and not vm_ram.available and state.available
    await hass.config_entries.async_unload(entry.entry_id)


async def test_vm_native_reboot_running_only_errors_refresh(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, _ = legacy(hass)
    snap = replace(inventory(), vms=(parse.Vm("Guest's VM; echo nope", 'running'),))
    await load(hass, monkeypatch, entry, snap)
    restart = loaded(hass, 'restart', 'vm')[0]
    fast = entry.runtime_data.coordinator
    fast.client.run = AsyncMock(return_value=CommandResult(0, '', ''))
    fast.async_request_refresh = AsyncMock()
    await restart.async_press()
    fast.client.run.assert_awaited_once_with("virsh reboot 'Guest'\"'\"'s VM; echo nope'", timeout=30)
    fast.async_request_refresh.assert_awaited_once()
    fast.client.run = AsyncMock(return_value=CommandResult(1, '', 'guest refused'))
    fast.async_request_refresh.reset_mock()
    with pytest.raises(HomeAssistantError):
        await restart.async_press()
    fast.async_request_refresh.assert_awaited_once()
    fast.async_set_updated_data(replace(snap, vms=(replace(snap.vms[0], state='shut_off'),)))
    await hass.async_block_till_done()
    assert not restart.available
    with pytest.raises(HomeAssistantError):
        await restart.async_press()
    assert fast.client.run.await_count == 1
    await hass.config_entries.async_unload(entry.entry_id)


async def test_metric_compose_identity_recovers_and_follows_recreation(hass, monkeypatch, tmp_path):
    hass.config.config_dir = str(tmp_path)
    entry, _, old = legacy(hass)
    dev = old.device('_stack_Stable')
    existing = old.entity('sensor', '_container_cpu_old-web', dev)
    await load(hass, monkeypatch, entry, compose_snapshot(replica=None))
    cpu = loaded(hass, 'cpu')[0]
    assert cpu.entity_id == existing.entity_id
    fast = entry.runtime_data.coordinator
    fast.async_set_updated_data(replace(compose_snapshot(), container_resources={'old-web': parse.ResourceSample(42)}))
    await hass.async_block_till_done()
    assert cpu.native_value == 42
    canonical = er.async_get(hass).async_get(cpu.entity_id).unique_id
    assert canonical.endswith('container_cpu_compose:["Stable","web",1]')
    fast.async_set_updated_data(replace(compose_snapshot('new-web'), container_resources={'new-web': parse.ResourceSample(77)}))
    await hass.async_block_till_done()
    assert loaded(hass, 'cpu') == [cpu] and cpu.native_value == 77
    assert cpu.extra_state_attributes['container_name'] == 'new-web'
    assert er.async_get(hass).async_get(cpu.entity_id).unique_id == canonical
    await hass.config_entries.async_unload(entry.entry_id)
