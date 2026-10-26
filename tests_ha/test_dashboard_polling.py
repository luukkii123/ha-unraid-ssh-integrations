"""Real coordinator scheduling, with SSH replaced at the transport boundary."""
from unittest.mock import AsyncMock
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.unraid_ssh.coordinator import UnraidCoordinator
from custom_components.unraid_ssh.ssh import CommandResult
from custom_components.unraid_ssh.const import POLL_TIMEOUT


async def test_dashboard_deadlines_are_independent_and_keep_outer_budget(hass,monkeypatch):
    entry=MockConfigEntry(domain='unraid_ssh',data={'host':'example.invalid'})
    entry.add_to_hass(hass)
    client=AsyncMock()
    client.run.return_value=CommandResult(0,'@@@ end\n','')
    coordinator=UnraidCoordinator(hass,entry,client)
    clock=[0]
    monkeypatch.setattr('custom_components.unraid_ssh.coordinator.monotonic',lambda:clock[0],raising=False)
    for moment,inventory,streams in [(0,True,True),(30,False,False),(60,False,True),(90,False,False),(300,True,True),(301,False,False)]:
        clock[0]=moment
        snapshot=await coordinator._async_update_data()
        coordinator.async_set_updated_data(snapshot)
        command=client.run.call_args.args[0]
        assert ('dashboard_launch dash_hardware ' in command) is inventory
        assert ('dashboard_launch dash_streams ' in command) is streams
        assert client.run.call_args.kwargs['timeout']==POLL_TIMEOUT==20
    await coordinator.async_shutdown()


async def test_optional_probe_failure_does_not_fail_main_poll(hass,monkeypatch):
    entry=MockConfigEntry(domain='unraid_ssh',data={'host':'example.invalid'})
    entry.add_to_hass(hass)
    client=AsyncMock()
    client.run.return_value=CommandResult(0,'@@@ stat\ncpu 1 0 0 9 0\n@@@ stat status=0\n@@@ end\n','')
    coordinator=UnraidCoordinator(hass,entry,client)
    snapshot=await coordinator._async_update_data()
    assert snapshot.cpu_times is not None
    assert snapshot.telemetry.hardware is None
    assert 'dash_hardware' in snapshot.telemetry.failed
    await coordinator.async_shutdown()


async def test_interface_id_survives_source_failure_but_prunes_after_successful_removal(hass,monkeypatch,tmp_path,freezer):
    from dataclasses import replace
    from homeassistant.helpers import entity_registry as er
    from custom_components.unraid_ssh.telemetry import DashboardTelemetry
    from custom_components.unraid_ssh.const import STALE_GRACE
    from test_device_migration import inventory,legacy,load,own_entities
    from test_orphan_cleanup import poll
    hass.config.config_dir=str(tmp_path)
    entry,_,_=legacy(hass)
    await load(hass,monkeypatch,entry,inventory())
    registry=er.async_get(hass)
    item=registry.async_get_or_create('sensor','unraid_ssh',entry.entry_id+'_net_rx_65746830',config_entry=entry)
    failed=replace(inventory(),telemetry=DashboardTelemetry(failed=frozenset({'dash_interfaces'})))
    await poll(hass,entry,failed)
    freezer.tick(STALE_GRACE*2)
    await poll(hass,entry,failed)
    assert registry.async_get(item.entity_id) is not None
    empty=replace(inventory(),telemetry=DashboardTelemetry(interfaces=()))
    await poll(hass,entry,empty)
    freezer.tick(STALE_GRACE*2)
    await poll(hass,entry,empty)
    assert registry.async_get(item.entity_id) is None
    await hass.config_entries.async_unload(entry.entry_id)
