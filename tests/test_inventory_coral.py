"""Kernel ABI fixtures are invented; USB presence never means inference ready."""
import json
import pytest
from unraid_ssh.inventory import parse_inventory_sections
from unraid_ssh.collect_inventory import inventory_sections
from test_inventory_usb import device


def pci(**kw):
    return dict(path='/sys/class/apex/apex_0',pci_address='0000:06:00.0',status='ALIVE',temp='49550',driver_version='1.2',**kw)


def parse(rows,usb=()):
    return parse_inventory_sections({'inv_coral':json.dumps(rows),'inv_usb':json.dumps(usb)})


def test_alive_millidegrees_source_and_two_ids():
    result=parse([pci(),{**pci(),'pci_address':'0000:07:00.0','path':'/sys/class/apex/apex_1'}])
    assert len(result.coral)==2
    assert result.coral[0].state=='ready' and result.coral[0].temperature_c==49.55
    assert result.coral[0].raw_status=='ALIVE' and result.coral[0].source=='pci_address'
    assert result.coral[0].id!=result.coral[1].id


@pytest.mark.parametrize('temp',['-50000','garbage','NaN','150001','inf'])
def test_temperature_invalid_or_shutdown_is_unknown(temp):
    assert parse([{**pci(),'temp':temp}]).coral[0].temperature_c is None


@pytest.mark.parametrize('raw,state',[('DEAD','error'),('LAMED','error'),('DRIVER_EXITING','error'),('new-state','unknown')])
def test_status_only_known_abi_values(raw,state):
    result=parse([{**pci(),'status':raw}]).coral[0]
    assert result.state==state and result.raw_status==raw


def test_empty_failure_and_fallback_identity():
    assert parse([]).coral==()
    assert parse_inventory_sections({}).coral is None
    row={**pci(),'pci_address':'','path':'/sys/class/apex/apex_3','realpath':'/private/topology'}
    result=parse([row]).coral[0]
    assert result.id=='/sys/class/apex/apex_3' and result.source=='sysfs_class'
    assert '/private' not in str(result)


@pytest.mark.parametrize('vid,pid',[('18d1','9302'),('1a6e','089a')])
def test_usb_coral_never_ready(vid,pid):
    result=parse([],[{**device(),'idVendor':vid,'idProduct':pid}]).coral[0]
    assert result.transport=='usb' and result.state=='unknown' and result.temperature_c is None
    assert result.raw_status is None and result.source=='usb_vid_pid'


def test_reader_does_not_open_accelerator_or_infer():
    command=dict(inventory_sections())['inv_coral']
    assert '/sys/class/apex' in command
    assert 'pci_address' in command and 'driver_version' in command
    assert 'fopen("/dev/' not in command and 'inference' not in command
