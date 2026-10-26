"""Dashboard entities are tested via real HA platform registration."""
from dataclasses import replace
import pytest
from custom_components.unraid_ssh.telemetry import DashboardTelemetry
from test_device_migration import inventory,legacy,load,own_entities,lookup


def dashboard():
    return DashboardTelemetry(cores=({'id':0,'percent':50},),power_packages=({'id':'package-0','power_w':12.5,'source':'rapl'},),memory={'total_bytes':1024,'available_bytes':512},hardware={'board_name':'Example Board'},gpu_info=(),registration='Basic',docker_mode='image',interfaces=({'id':'6574682e30','name':'eth.0','rx_bytes_s':100,'tx_bytes_s':200},{'id':'6574685f30','name':'eth_0','rx_bytes_s':300,'tx_bytes_s':400}),storage=({'kind':'boot','total_bytes':100,'used_bytes':20,'free_bytes':80,'percent':20},),pools=(),shares=(),vms=(),smart=({'name':'disk1','status':'warning','sampled_at':123,'source':'emhttp_cache','stale':True,'details':[{'field':'Current_Pending_Sector','value':2}]},),sample_times={'dash_cpu':10,'dash_memory':10,'dash_hardware':10},known_ids={'dash_interfaces':('6574682e30','6574685f30'),'dash_smart':('disk1',)})


async def setup_dashboard(hass,monkeypatch,tmp_path):
    hass.config.config_dir=str(tmp_path)
    entry,_,_=legacy(hass)
    snap=replace(inventory(),telemetry=dashboard())
    await load(hass,monkeypatch,entry,snap)
    return entry,snap


def state(hass,entry,suffix):
    return hass.states.get(own_entities(hass,entry)['_'+suffix].entity_id)


async def test_dashboard_metadata_measurements_and_collision_free_interfaces(hass,monkeypatch,tmp_path):
    entry,snap=await setup_dashboard(hass,monkeypatch,tmp_path)
    try:
        entities=own_entities(hass,entry)
        server=lookup(hass,entry,'').id
        for suffix in ('dashboard_cpu','dashboard_memory','dashboard_hardware','dashboard_interfaces','dashboard_storage','dashboard_shares','dashboard_vms'):
            assert entities['_'+suffix].device_id==server
            current=state(hass,entry,suffix)
            assert current.state=='ok'
            assert current.attributes['schema_version']==1
        assert state(hass,entry,'dashboard_cpu').attributes['cores']==({'id':0,'percent':50},)
        for suffix,value,unit in [('cpu_power',12.5,'W'),('boot_usage',20,'%'),('net_rx_6574682e30',100,'B/s'),('net_tx_6574685f30',400,'B/s')]:
            current=state(hass,entry,suffix)
            assert float(current.state)==value
            assert current.attributes['unit_of_measurement']==unit
            assert current.attributes['state_class']=='measurement'
        assert entities['_net_rx_6574682e30'].entity_id!=entities['_net_rx_6574685f30'].entity_id
        assert entities['_disk_smart_disk1'].device_id==lookup(hass,entry,'_disk_disk1').id
        assert state(hass,entry,'disk_smart_disk1').state=='warning'
        assert state(hass,entry,'disk_smart_disk1').attributes['stale'] is True
        assert 'serial' not in str(state(hass,entry,'dashboard_hardware').attributes)
        before=entities['_net_rx_6574682e30'].entity_id
        lost=replace(snap,telemetry=replace(dashboard(),interfaces=None,power_packages=None,failed=frozenset({'dash_interfaces','dash_power'})))
        entry.runtime_data.coordinator.async_set_updated_data(lost)
        await hass.async_block_till_done()
        assert own_entities(hass,entry)['_net_rx_6574682e30'].entity_id==before
        assert state(hass,entry,'net_rx_6574682e30').state=='unavailable'
        assert state(hass,entry,'cpu_power').state=='unavailable'
        assert state(hass,entry,'dashboard_cpu').state=='partial'
    finally:
        await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize('language,want',[('en','CPU power'),('de','CPU-Leistung')])
async def test_dashboard_names_are_translated(hass,monkeypatch,tmp_path,language,want):
    hass.config.language=language
    entry,_=await setup_dashboard(hass,monkeypatch,tmp_path)
    try:
        assert state(hass,entry,'cpu_power').name=='Example Unraid '+want
    finally:
        await hass.config_entries.async_unload(entry.entry_id)
