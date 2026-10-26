"""Real HA entities and scheduling with invented inventory snapshots."""
from dataclasses import replace
from unittest.mock import AsyncMock
import json
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from custom_components.unraid_ssh.inventory import DashboardInventory,UserAccess,UsbDevice,CoralDevice
from custom_components.unraid_ssh.coordinator import UnraidCoordinator
from custom_components.unraid_ssh.ssh import CommandResult
from test_device_migration import inventory,legacy,load,own_entities,lookup
from test_dashboard_entities import state

PCI='0000:06:00.0'
HEX=PCI.encode().hex()


def sample():
    return DashboardInventory(users=(UserAccess('Ada','Text',False,2,1),),smb_enabled=True,
        usb=(UsbDevice('1-2','1-2','USB','1234','5678',480,False,'host'),),
        coral=(CoralDevice(PCI,'Coral TPU 1','pci','ready',49.55,'1','ALIVE','/sys/class/apex/apex_0/pci_address'),),
        sample_times={'inv_users':1,'inv_usb':1,'inv_usb_mapping':1,'inv_coral':1},known_coral_ids=(PCI,))


async def setup_inventory(hass,monkeypatch,tmp_path):
    hass.config.config_dir=str(tmp_path)
    entry,_,_=legacy(hass)
    snap=replace(inventory(),inventory=sample())
    await load(hass,monkeypatch,entry,snap)
    return entry,snap


async def test_inventory_numeric_counts_measurements_and_failure(hass,monkeypatch,tmp_path):
    entry,snap=await setup_inventory(hass,monkeypatch,tmp_path)
    try:
        entities=own_entities(hass,entry)
        for suffix in ('dashboard_users','dashboard_usb','dashboard_coral'):
            current=state(hass,entry,suffix)
            assert current.state=='1'
            assert current.attributes['schema_version']==1
            assert entities['_'+suffix].device_id==lookup(hass,entry,'').id
        assert state(hass,entry,'dashboard_users').attributes['unprotected_count']==1
        assert state(hass,entry,'dashboard_users').attributes['users'][0]['name']=='Ada'
        temp=state(hass,entry,'coral_temp_'+HEX)
        assert temp.state=='49.55' and temp.attributes['unit_of_measurement']=='°C'
        assert temp.attributes['state_class']=='measurement'
        status=state(hass,entry,'coral_status_'+HEX)
        assert status.state=='ready' and status.attributes['options']==['ready','error','unknown']
        failed=replace(snap,inventory=replace(sample(),users=None,coral=None,failed=frozenset({'inv_users','inv_coral','inv_usb_mapping'}),mapping_status='partial'))
        entry.runtime_data.coordinator.async_set_updated_data(failed)
        await hass.async_block_till_done()
        assert state(hass,entry,'dashboard_users').state=='unavailable'
        assert state(hass,entry,'dashboard_coral').state=='unavailable'
        assert state(hass,entry,'dashboard_usb').state=='1'
        assert state(hass,entry,'dashboard_usb').attributes['mapping_status']=='partial'
        assert state(hass,entry,'coral_temp_'+HEX).state=='unavailable'
        empty=replace(snap,inventory=DashboardInventory(users=(),usb=(),coral=()))
        entry.runtime_data.coordinator.async_set_updated_data(empty)
        await hass.async_block_till_done()
        assert all(state(hass,entry,key).state=='0' for key in ('dashboard_users','dashboard_usb','dashboard_coral'))
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_inventory_deadlines_three_skips_error_and_recovery_at_ten_seconds(hass,monkeypatch):
    entry=MockConfigEntry(domain='unraid_ssh',data={'host':'example.invalid','scan_interval':10}); entry.add_to_hass(hass)
    client=AsyncMock(); coordinator=UnraidCoordinator(hass,entry,client)
    clock=[0]; monkeypatch.setattr('custom_components.unraid_ssh.coordinator.monotonic',lambda:clock[0])
    users=json.dumps(dict(users=[dict(name='Ada',passwd='yes')],var=dict(shareSMBEnabled='no'),rights={},shares=[],disks=[]))
    def output(include_users=True,include_mapping=True):
        rows={'inv_usb':'[]','inv_coral':'[]'}
        if include_users: rows['inv_users']=users
        if include_mapping: rows['inv_usb_mapping']='{"supported":false,"active":[]}'
        return ''.join('@@@ '+k+'\n'+v+'\n@@@ '+k+' status=0\n' for k,v in rows.items())+'@@@ end\n'
    for moment,udue,mdue,ufail in [(0,True,True,False),(10,False,False,False),(20,False,False,False),(30,False,False,False),(60,False,True,False),(70,False,False,False),(80,False,False,False),(90,False,False,False),(120,False,True,False),(300,True,True,True),(310,False,False,True),(320,False,False,True),(330,False,False,True),(600,True,True,False)]:
        clock[0]=moment
        client.run.return_value=CommandResult(0,output(udue and moment!=300,mdue and moment!=60),'')
        snap=await coordinator._async_update_data(); coordinator.async_set_updated_data(snap)
        command=client.run.call_args.args[0]
        assert ('dashboard_launch inv_users ' in command) is udue
        assert ('dashboard_launch inv_usb_mapping ' in command) is mdue
        assert 'dashboard_launch inv_usb ' in command and 'dashboard_launch inv_coral ' in command
        assert client.run.call_args.kwargs['timeout']==20
        assert (snap.inventory.users is None) is ufail
        if ufail: assert 'inv_users' in snap.inventory.failed
        assert ('inv_usb_mapping' in snap.inventory.failed) is (60 <= moment < 120)
        assert snap.inventory.sample_times['inv_users'] == (0 if moment < 300 else 300 if moment < 600 else 600)
    await coordinator.async_shutdown()


async def test_coral_outage_keeps_ids_confirmed_removal_prunes(hass,monkeypatch,tmp_path,freezer):
    from homeassistant.helpers import entity_registry as er
    from custom_components.unraid_ssh.const import STALE_GRACE
    from test_orphan_cleanup import poll
    entry,snap=await setup_inventory(hass,monkeypatch,tmp_path)
    try:
        registry=er.async_get(hass); item=own_entities(hass,entry)['_coral_temp_'+HEX]
        lost=replace(snap,inventory=replace(sample(),coral=None,failed=frozenset({'inv_coral'})))
        await poll(hass,entry,lost); freezer.tick(STALE_GRACE*2); await poll(hass,entry,lost)
        assert registry.async_get(item.entity_id) is not None
        empty=replace(snap,inventory=DashboardInventory(coral=()))
        await poll(hass,entry,empty); freezer.tick(STALE_GRACE*2); await poll(hass,entry,empty)
        assert registry.async_get(item.entity_id) is None
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('previous',[False,True])
@pytest.mark.parametrize('failure',['ssh','truncated'])
async def test_due_ssh_failure_does_not_revive_inventory_on_skipped_poll(hass,monkeypatch,previous,failure):
    from homeassistant.helpers.update_coordinator import UpdateFailed
    from custom_components.unraid_ssh.ssh import SSHError
    entry=MockConfigEntry(domain='unraid_ssh',data={'host':'example.invalid'}); entry.add_to_hass(hass)
    client=AsyncMock(); coordinator=UnraidCoordinator(hass,entry,client)
    clock=[0]; monkeypatch.setattr('custom_components.unraid_ssh.coordinator.monotonic',lambda:clock[0])
    if previous: coordinator.async_set_updated_data(replace(inventory(),inventory=sample()))
    # A due read failed at the transport boundary; the next poll skips users.
    clock[0]=300
    if failure=='ssh': client.run.side_effect=SSHError('synthetic failure')
    else: client.run.return_value=CommandResult(0,'@@@ inv_users\npartial','')
    with pytest.raises(UpdateFailed): await coordinator._async_update_data()
    clock[0]=310; client.run.side_effect=None; client.run.return_value=CommandResult(0,'@@@ inv_usb\n[]\n@@@ inv_coral\n[]\n@@@ end\n','')
    result=await coordinator._async_update_data()
    assert result.inventory.users is None and 'inv_users' in result.inventory.failed
    await coordinator.async_shutdown()


async def test_inventory_two_entries_are_isolated(hass,monkeypatch,tmp_path):
    entry,snap=await setup_inventory(hass,monkeypatch,tmp_path)
    other=MockConfigEntry(domain='unraid_ssh',title='Second server',data={'host':'second.invalid'}); other.add_to_hass(hass)
    await load(hass,monkeypatch,other,replace(snap,inventory=DashboardInventory(users=(),usb=(),coral=())))
    try:
        assert state(hass,entry,'dashboard_users').state=='1'
        assert state(hass,other,'dashboard_users').state=='0'
        assert own_entities(hass,entry)['_dashboard_users'].entity_id!=own_entities(hass,other)['_dashboard_users'].entity_id
        assert own_entities(hass,entry)['_dashboard_usb'].device_id!=own_entities(hass,other)['_dashboard_usb'].device_id
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.config_entries.async_unload(other.entry_id)
