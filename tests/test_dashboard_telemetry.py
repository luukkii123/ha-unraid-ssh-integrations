"""Dashboard contracts use invented counters and identifiers only."""
import json
import pytest
from unraid_ssh import telemetry
from unraid_ssh.collect import build_state_command


def sample(**updates):
    sections = {'dash_cpu': 'cpu0 100 0 20 300 10 0 0 0 30 0\nbtime 7\n',
                'dash_memory': 'MemTotal: 1000 kB\nMemAvailable: 600 kB\nMemFree: 200 kB\nBuffers: 50 kB\nCached: 300 kB\nSReclaimable: 100 kB\nShmem: 50 kB\n',
                'dash_interfaces': json.dumps([{'ifindex': 2, 'ifname': 'eth.0', 'mtu':1500,'operstate':'UP', 'stats64':{'rx':{'bytes':100},'tx':{'bytes':200}}}]),
                'dash_power': 'package-0\tpackage-0\t9000000\t10000000\npackage-0:0\tcore\t100\t1000\n',
                'dash_storage': json.dumps([{'target':'/boot','size':100,'used':25,'avail':75},{'target':'/var/lib/docker','size':1000,'used':100,'avail':900}]),
                'dash_mounts':'1 0 1:1 / /boot rw - vfat /dev/fake rw\n',
                'dash_hardware':json.dumps({'board_name':'Imaginary Board','serial':'SECRET'}),
                'dash_smart':json.dumps({'disk1':{'text':'SMART overall-health self-assessment test result: PASSED','mtime':123,'stale':True}}),
                'dash_pools':'[]', 'dash_shares':'["Example"]\nsecurity="private"\nexport="yes"',
                'shares':'["Example"]\nname="Example"', 'dash_streams':'{"Example":0}', 'dash_gpu':'0, 99.1',
                'var':'regTy="Basic"', 'disks':'', 'vms':'', 'vm_stats':''}
    sections.update(updates)
    return sections


def test_cpu_guest_hotplug_and_memory_partition():
    first=telemetry.parse_dashboard_sections(sample(),None,sampled_at=10)
    assert first.cores == ({'id':0,'percent':None},)
    second=telemetry.parse_dashboard_sections(sample(dash_cpu='cpu0 150 0 20 350 10 0 0 0 50 0\nbtime 7'),first,sampled_at=12)
    assert second.cores == ({'id':0,'percent':50.0},)
    assert second.memory == {'total_bytes':1024000,'available_bytes':614400,'free_bytes':204800,'cache_bytes':358400,'buffers_bytes':51200,'system_bytes':409600}
    gone=telemetry.parse_dashboard_sections(sample(dash_cpu='btime 7'),second,sampled_at=14)
    back=telemetry.parse_dashboard_sections(sample(),gone,sampled_at=16)
    assert back.cores[0]['percent'] is None
    bad=telemetry.parse_dashboard_sections(sample(dash_memory='MemTotal: 1 kB\nMemFree: 5 kB'),None,sampled_at=1)
    assert bad.memory is None


def test_power_wrap_reboot_and_interfaces_identity():
    first=telemetry.parse_dashboard_sections(sample(),None,sampled_at=10)
    second=telemetry.parse_dashboard_sections(sample(dash_power='package-0\tpackage-0\t1000000\t10000000',dash_interfaces=json.dumps([{'ifindex':2,'ifname':'eth.0','stats64':{'rx':{'bytes':300},'tx':{'bytes':600}}}])),first,sampled_at=12)
    assert second.power_packages == ({'id':'package-0','power_w':1.0,'source':'rapl'},)
    assert second.interfaces[0]['rx_bytes_s']==100
    changed=telemetry.parse_dashboard_sections(sample(dash_interfaces=json.dumps([{'ifindex':3,'ifname':'eth.0','stats64':{'rx':{'bytes':400},'tx':{'bytes':800}}}])),second,sampled_at=14)
    assert changed.interfaces[0]['rx_bytes_s'] is None
    reboot=telemetry.parse_dashboard_sections(sample(dash_cpu='cpu0 200 0 20 400 10 0 0 0\nbtime 8'),first,sampled_at=12)
    assert reboot.power_packages[0]['power_w'] is None
    assert telemetry.interface_key('eth.0') == '6574682e30'
    assert telemetry.interface_key('eth_0') == '6574685f30'


@pytest.mark.parametrize('text,want',[('SMART overall-health self-assessment test result: PASSED','passed'),('SMART Health Status: NOK','failed'),('SMART overall-health self-assessment test result: FAILED','failed'),('unrecognized','unknown'),('SMART overall-health self-assessment test result: PASSED\n197 Current_Pending_Sector 0x0032 100 100 000 Old_age Always - 2','warning')])
def test_smart_cache_and_privacy(text,want):
    data=telemetry.parse_dashboard_sections(sample(dash_smart=json.dumps({'disk1':{'text':text,'mtime':123,'stale':True}})),None,sampled_at=1)
    assert data.smart[0]['status']==want
    assert data.smart[0]['stale'] is True
    assert 'serial' not in data.hardware
    assert [row['kind'] for row in data.storage] == ['boot']


def test_due_omission_retains_timestamp_failure_is_unknown_and_empty_is_empty():
    first=telemetry.parse_dashboard_sections(sample(),None,sampled_at=10)
    sections=sample(); sections.pop('dash_hardware'); sections.pop('dash_streams')
    second=telemetry.parse_dashboard_sections(sections,first,sampled_at=30)
    assert second.hardware==first.hardware
    assert second.sample_times['dash_hardware']==10
    sections['dash_requested']='dash_hardware\ndash_streams'
    failed=telemetry.parse_dashboard_sections(sections,second,sampled_at=70)
    assert failed.hardware is None
    assert failed.shares[0]['streams'] is None
    assert 'dash_streams' in failed.failed
    empty=telemetry.parse_dashboard_sections(sample(dash_interfaces='[]'),first,sampled_at=40)
    assert empty.interfaces==()


def test_vm_device_matching_runtime_and_hotplug():
    def vm(rx,tx,read,write,runtime='3'):
        return sample(vms=f' Id Name State\n--------------------\n {runtime}  ExampleVM  running\n',vm_stats=f"Domain: 'ExampleVM'\n cpu.time={rx*1000000000}\n net.0.name=vnet0\n net.0.rx.bytes={rx}\n net.0.tx.bytes={tx}\n net.1.name=vnet1\n net.1.rx.bytes={rx}\n net.1.tx.bytes={tx}\n block.0.name=vda\n block.0.rd.bytes={read}\n block.0.wr.bytes={write}\n")
    first=telemetry.parse_dashboard_sections(vm(10,20,30,40),None,sampled_at=10)
    second=telemetry.parse_dashboard_sections(vm(30,60,90,120),first,sampled_at=12)
    assert second.vms[0]['net_rx_bytes_s']==20
    assert second.vms[0]['disk_write_bytes_s']==40
    restarted=telemetry.parse_dashboard_sections(vm(40,80,120,160,'4'),second,sampled_at=14)
    assert restarted.vms[0]['net_rx_bytes_s'] is None


def test_dashboard_command_groups_and_fixed_sources():
    fast=build_state_command()
    slow=build_state_command(include_inventory=True, include_streams=True)
    assert 'dash_cpu' in fast
    assert 'dash_hardware status=' not in fast
    assert 'dash_hardware status=' in slow
    assert '--interface --block' in fast
    assert 'smartctl' not in slow
    assert 'optionCorrect' not in slow


def test_pool_capacity_only_filesystem_carrier_and_sanitized_members():
    data=telemetry.parse_dashboard_sections(sample(dash_pools=json.dumps([{'name':'pool-example','filesystem':'btrfs','state':'degraded','members':['device-a','device-b'],'total_bytes':10240,'used_bytes':2048,'free_bytes':8192,'raw':'secret'}])),None,sampled_at=1)
    assert data.pools == ({'name':'pool-example','filesystem':'btrfs','state':'degraded','members':['device-a','device-b'],'total_bytes':10240,'used_bytes':2048,'free_bytes':8192},)


def test_counter_reset_and_device_hotplug_never_publish_negative_or_zero():
    first=telemetry.parse_dashboard_sections(sample(),None,sampled_at=1)
    reset=telemetry.parse_dashboard_sections(sample(dash_interfaces=json.dumps([{'ifindex':2,'ifname':'eth.0','stats64':{'rx':{'bytes':1},'tx':{'bytes':2}}}])),first,sampled_at=2)
    assert reset.interfaces[0]['rx_bytes_s'] is None
    row={'net.0.name':'vnet0','net.0.rx.bytes':20,'net.1.name':'vnet1','net.1.rx.bytes':10}
    assert telemetry._device_rate(row,{'net.0.name':'vnet0','net.0.rx.bytes':1},'net','rx.bytes',1) is None


@pytest.mark.parametrize('mode,want',[('image',('docker_vdisk',)),('folder',()),('unexpected',())])
def test_docker_capacity_requires_image_mode_and_actual_mount(mode,want):
    data=telemetry.parse_dashboard_sections(sample(dash_docker=f'DOCKER_IMAGE_TYPE="{mode}"',dash_mounts='1 0 1:1 / /var/lib/docker rw - btrfs /dev/loop0 rw'),None,sampled_at=1)
    assert tuple(row['kind'] for row in data.storage)==want
    assert data.docker_mode==(mode if mode in {'image','folder'} else 'unknown')
