"""All actions use a fake SSH transport and actual HA button entities."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock
import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import entity_platform
from custom_components.unraid_ssh.parse import parse_var
from custom_components.unraid_ssh.ssh import CommandResult,SSHError
from test_device_migration import inventory,legacy,load,own_entities

ACTIONS=('server_reboot','server_shutdown','array_start','array_stop','parity_start','parity_pause','parity_resume','parity_cancel','spindown_all','mover_start')


def safe_array(**changes):
    fields={'mdState':'STARTED','fsState':'Started','configValid':'yes','startMode':'Normal','mdNumMissing':'0','mdResync':'0','mdResyncPos':'0','mdResyncAction':'check','mdResyncCorr':'0','shareMoverActive':'no'}
    fields.update(changes)
    return parse_var('\n'.join(f'{k}="{v}"' for k,v in fields.items()))


async def setup_buttons(hass,monkeypatch,tmp_path):
    hass.config.config_dir=str(tmp_path)
    entry,_,_=legacy(hass)
    snapshot=replace(inventory(),array=safe_array())
    await load(hass,monkeypatch,entry,snapshot)
    return entry,snapshot


def button(hass,entry,suffix):
    identifier=own_entities(hass,entry)['_'+suffix].entity_id
    return next(platform.entities[identifier] for platform in entity_platform.async_get_platforms(hass,'unraid_ssh') if identifier in platform.entities)


async def test_server_buttons_register_once_and_poll_before_sending(hass,monkeypatch,tmp_path):
    entry,snapshot=await setup_buttons(hass,monkeypatch,tmp_path)
    try:
        entities=own_entities(hass,entry)
        assert all('_'+action in entities for action in ACTIONS)
        before={key:entities['_'+key].entity_id for key in ACTIONS}
        fast=entry.runtime_data.coordinator
        events=[]
        async def poll(): events.append('poll'); return snapshot
        async def send(command,*,timeout):
            events.append((command,timeout)); return CommandResult(0,'','')
        fast._async_update_data=poll
        fast.client.run=send
        await button(hass,entry,'server_reboot').async_press()
        assert events[:2]==['poll',('/sbin/reboot',30)]
        await hass.async_block_till_done()
        assert all(own_entities(hass,entry)['_'+key].entity_id==value for key,value in before.items())
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('failure',[SSHError('secret transport detail'),TimeoutError(),CommandResult(1,'','secret command detail')])
async def test_failed_action_never_retries_or_claims_success(hass,monkeypatch,tmp_path,failure):
    entry,_=await setup_buttons(hass,monkeypatch,tmp_path)
    try:
        client=entry.runtime_data.client
        client.run=AsyncMock(side_effect=failure) if isinstance(failure,Exception) else AsyncMock(return_value=failure)
        with pytest.raises(HomeAssistantError) as caught:
            await button(hass,entry,'server_shutdown').async_press()
        assert 'secret' not in str(caught.value)
        assert client.run.await_count==1
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_busy_action_and_update_admission_are_mutually_exclusive(hass,monkeypatch,tmp_path):
    from custom_components.unraid_ssh.actions import ActionError
    entry,snapshot=await setup_buttons(hass,monkeypatch,tmp_path)
    try:
        fast=entry.runtime_data.coordinator
        entered=asyncio.Event(); release=asyncio.Event()
        async def poll(): entered.set(); await release.wait(); return snapshot
        fast._async_update_data=poll
        fast.client.run=AsyncMock(return_value=CommandResult(0,'',''))
        pending=asyncio.create_task(button(hass,entry,'server_reboot').async_press())
        await entered.wait()
        with pytest.raises(HomeAssistantError): await button(hass,entry,'server_shutdown').async_press()
        with pytest.raises(ActionError): await entry.runtime_data.queue.enqueue([snapshot.containers[0]])
        release.set(); await pending
        assert fast.client.run.await_count==1
        assert entry.runtime_data.queue.jobs==[]
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


async def test_active_updatequeue_mover_and_failed_fresh_poll_block_actions(hass,monkeypatch,tmp_path):
    entry,snapshot=await setup_buttons(hass,monkeypatch,tmp_path)
    try:
        fast=entry.runtime_data.coordinator
        fast.client.run=AsyncMock(return_value=CommandResult(0,'',''))
        monkeypatch.setattr(entry.runtime_data.queue,'summary',lambda:{'active':True,'blocked':False})
        with pytest.raises(HomeAssistantError): await button(hass,entry,'server_reboot').async_press()
        monkeypatch.setattr(entry.runtime_data.queue,'summary',lambda:{'active':False,'blocked':False})
        fast._async_update_data=AsyncMock(return_value=replace(snapshot,array=safe_array(shareMoverActive='yes')))
        with pytest.raises(HomeAssistantError): await button(hass,entry,'mover_start').async_press()
        from homeassistant.helpers.update_coordinator import UpdateFailed
        fast._async_update_data=AsyncMock(side_effect=UpdateFailed('offline'))
        with pytest.raises(HomeAssistantError): await button(hass,entry,'server_reboot').async_press()
        assert fast.client.run.await_count==0
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('fields,want',[({'mdResync':'100','mdResyncPos':'10'},'running'),({'mdResyncPos':'10'},'paused'),({'mdResyncAction':'recon'},'other'),({'mdResync':'bad'},'unknown')])
async def test_parity_binary_sensor_exposes_explicit_operation_state(hass,monkeypatch,tmp_path,fields,want):
    entry,snapshot=await setup_buttons(hass,monkeypatch,tmp_path)
    try:
        entry.runtime_data.coordinator.async_set_updated_data(replace(snapshot,array=safe_array(**fields)))
        await hass.async_block_till_done()
        current=hass.states.get(own_entities(hass,entry)['_parity_running'].entity_id)
        assert current.attributes['state']==want
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
