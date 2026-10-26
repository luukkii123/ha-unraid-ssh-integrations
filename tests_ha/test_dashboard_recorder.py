"""Query Recorder's real DB, not only the entity class exclusion flags."""
from dataclasses import replace
import json
import pytest
import logging
from sqlalchemy import select
from homeassistant.components.recorder.db_schema import States, StatesMeta, StateAttributes
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done
from test_dashboard_entities import setup_dashboard,state
from test_device_migration import own_entities


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url):
    """Prepare DB fixtures before the globally enabled integration fixture uses HA."""
    yield


async def test_dashboard_lists_stay_live_but_are_absent_from_recorded_attributes(recorder_mock,hass,monkeypatch,tmp_path):
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    entry,snapshot=await setup_dashboard(hass,monkeypatch,tmp_path)
    try:
        await async_wait_recording_done(hass)
        changed=replace(snapshot,telemetry=replace(snapshot.telemetry,cores=({'id':0,'percent':75},),power_packages=({'id':'package-0','power_w':20,'source':'rapl'},)))
        entry.runtime_data.coordinator.async_set_updated_data(changed)
        await async_wait_recording_done(hass)
        assert state(hass,entry,'dashboard_cpu').attributes['cores']==({'id':0,'percent':75},)
        ids={suffix:item.entity_id for suffix,item in own_entities(hass,entry).items()}
        def recorded():
            with recorder_mock.get_session() as session:
                return list(session.execute(select(StatesMeta.entity_id,States.state,StateAttributes.shared_attrs).join(States,States.metadata_id==StatesMeta.metadata_id).join(StateAttributes,States.attributes_id==StateAttributes.attributes_id)))
        rows=await hass.async_add_executor_job(recorded)
        cpu=[json.loads(attrs) for entity,_,attrs in rows if entity==ids['_dashboard_cpu']]
        assert cpu
        assert all('schema_version' in attrs for attrs in cpu)
        for entity,_,attrs in rows:
            if entity in {v for k,v in ids.items() if k.startswith('_dashboard_')}:
                values=json.loads(attrs)
                assert not set(values) & {'cores','packages','memory','hardware','registration','gpus','interfaces','filesystems','pools','docker_mode','shares','vms','sampled_at','failed_sections'}
            if entity==ids['_disk_smart_disk1']:
                assert 'details' not in json.loads(attrs)
        power=[value for entity,value,_ in rows if entity==ids['_cpu_power']]
        assert '12.5' in power and '20' in power
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
