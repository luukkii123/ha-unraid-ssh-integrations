"""Inventory list updates stay live without recording identities or metadata."""
from dataclasses import replace
import json
import logging
import pytest
from sqlalchemy import select
from homeassistant.components.recorder.db_schema import States,StatesMeta,StateAttributes
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done
from test_inventory_entities import setup_inventory,HEX
from test_dashboard_entities import state
from test_device_migration import own_entities


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url):
    yield


async def test_inventory_lists_excluded_temperature_recorded(recorder_mock,hass,monkeypatch,tmp_path):
    logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
    entry,snap=await setup_inventory(hass,monkeypatch,tmp_path)
    try:
        await async_wait_recording_done(hass)
        changed=replace(snap,inventory=replace(snap.inventory,
            users=(replace(snap.inventory.users[0],description='Changed text'),),
            usb=(replace(snap.inventory.usb[0],name='Changed USB'),),
            coral=(replace(snap.inventory.coral[0],temperature_c=51),),sample_times={'inv_coral':42,'inv_users':42}))
        entry.runtime_data.coordinator.async_set_updated_data(changed)
        await async_wait_recording_done(hass)
        assert state(hass,entry,'dashboard_users').attributes['users'][0]['description']=='Changed text'
        assert state(hass,entry,'dashboard_usb').attributes['devices'][0]['name']=='Changed USB'
        ids={suffix:item.entity_id for suffix,item in own_entities(hass,entry).items()}
        def recorded():
            with recorder_mock.get_session() as session:
                return list(session.execute(select(StatesMeta.entity_id,States.state,StateAttributes.shared_attrs).join(States,States.metadata_id==StatesMeta.metadata_id).join(StateAttributes,States.attributes_id==StateAttributes.attributes_id)))
        rows=await hass.async_add_executor_job(recorded)
        inventory_ids={ids['_dashboard_'+kind] for kind in ('users','usb','coral')}
        assert inventory_ids <= {entity for entity,_,_ in rows}
        for entity,_,attrs in rows:
            if entity in inventory_ids:
                assert not set(json.loads(attrs)) & {'users','devices','sampled_at','failed_sections','smb_enabled','mapping_status','unprotected_count'}
        temps=[value for entity,value,_ in rows if entity==ids['_coral_temp_'+HEX]]
        assert '49.55' in temps and '51' in temps
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
