"""Only current sysfs devices and active hostdev evidence may assign USB."""
from dataclasses import asdict
import json
import pytest
from unraid_ssh.inventory import parse_inventory_sections
from unraid_ssh.collect_inventory import inventory_sections


def device(port='1-2',bus='1',dev='4',**kw):
    return dict(port=port,busnum=bus,devnum=dev,idVendor='1234',idProduct='abcd',product='Example USB',manufacturer='Example',bDeviceClass='00',speed='480',authorized='1',driver='usb',**kw)


def parse(devices,mapping=None):
    sections={'inv_usb':json.dumps(devices)}
    if mapping is not None: sections['inv_usb_mapping']=json.dumps(mapping)
    return parse_inventory_sections(sections)


def mapping(*rows):
    return dict(supported=True,active=list(rows))


def test_usb_projection_interface_hub_and_exact_active_vm():
    row=device(serial='PRIVATE')
    result=parse([row,device('1-2:1.0'),{**device('1-3'), 'bDeviceClass':'09'}],mapping(dict(vm='VM A',bus='1',dev='4')))
    assert len(result.usb)==2
    first=result.usb[0]
    assert first.state=='vm' and first.assigned_vm=='VM A'
    assert first.assignment_source=='libvirt_address'
    assert result.usb[1].is_hub
    assert first.speed_mbps==480
    assert set(asdict(first))=={'id','port','name','vendor_id','product_id','speed_mbps','is_hub','state','assigned_vm','remote_host','assignment_source'}
    assert 'PRIVATE' not in str(asdict(first))


def test_vid_pid_requires_unique_device_and_unique_assignment():
    record=dict(vm='VM B',vendor='1234',product='abcd')
    assert parse([device()],mapping(record)).usb[0].assigned_vm=='VM B'
    assert all(d.state=='unknown' for d in parse([device(),device('1-3','1','5')],mapping(record)).usb)
    assert parse([device()],mapping(dict(vm='A',bus='1',dev='4'),dict(vm='B',bus='1',dev='4'))).usb[0].state=='unknown'


def test_cache_cannot_invent_vm_hotplug_or_phantom():
    cache=dict(port='1-2',bus='1',dev='3',connected='1',VM='stale VM')
    result=parse([device()],dict(supported=True,active=[],cache=[cache]))
    assert result.usb[0].state=='host' and result.usb[0].assigned_vm is None
    assert parse([],dict(supported=True,active=[],cache=[cache])).usb==()


@pytest.mark.parametrize('status,want',[('1','usbip_bound'),('2','remote'),('0','host')])
def test_usbip_bound_vs_connected_remote_target_unknown(status,want):
    result=parse([device(usbip_status=status)],dict(supported=False,active=[],remote=[dict(port='1-2',hostname='synthetic.invalid',IP='192.0.2.7')]))
    assert result.usb[0].state==want
    assert result.usb[0].remote_host is None


def test_missing_plugin_inventory_readable_mapping_error_partial():
    assert parse([device()],dict(supported=False,active=[])).mapping_status=='unsupported'
    result=parse([device()])
    assert result.usb is not None and result.mapping_status=='partial'
    assert 'inv_usb_mapping' in result.failed
    assert parse_inventory_sections({}).usb is None
    assert parse([{**device(),'authorized':'0'}],mapping()).usb[0].state=='unauthorized'


def test_reader_whitelist_and_no_mutating_command():
    commands=dict(inventory_sections())
    assert {'inv_usb','inv_usb_mapping'} <= commands.keys()
    assert '/serial' not in commands['inv_usb']
    assert 'dumpxml' in commands['inv_usb_mapping']
    assert 'escapeshellarg' in commands['inv_usb_mapping']
    for command in commands.values():
        assert not any(word in command for word in ('attach-device','detach-device','usbip list','modprobe','include(','require('))


def test_actual_php_readers_with_synthetic_files_only(tmp_path):
    import subprocess
    import shlex
    base=tmp_path/'emhttp'; base.mkdir()
    for name,text in {'users.ini':'[Ada]\nname="Ada"\ndesc="<tag>"\npasswd="yes"\nsecret="do-not-export"\n','var.ini':'shareSMBEnabled="yes"\nshareDisk="no"\nfsState="Started"\nprivate="do-not-export"\n','sec.ini':'[Docs]\nexport="e"\nsecurity="public"\nreadList="Ada"\nwriteList="Ada"\nsecret="do-not-export"\n','shares.ini':'[Docs]\nname="Docs"\n','disks.ini':'[disk1]\nname="disk1"\nserial="do-not-export"\n'}.items():
        (base/name).write_text(text)
    usb=tmp_path/'usb'; usb.mkdir(); dev=usb/'1-2'; dev.mkdir()
    for key,value in device().items():
        if key!='port': (dev/key).write_text(str(value))
    (dev/'serial').write_text('do-not-export')
    apex=tmp_path/'apex'; apex.mkdir(); item=apex/'apex_0'; item.mkdir()
    for key,value in dict(status='ALIVE',temp='49550',pci_address='0000:06:00.0',driver_version='1').items(): (item/key).write_text(value)
    commands=dict(inventory_sections()); outputs={}
    for key in ('inv_users','inv_usb','inv_coral'):
        command=commands[key].replace('/var/local/emhttp',str(base)).replace('/sys/bus/usb/devices',str(usb)).replace('/sys/class/apex',str(apex))
        outputs[key]=subprocess.check_output(shlex.split(command),text=True)
        assert 'do-not-export' not in outputs[key]
    assert json.loads(outputs['inv_users'])['users'][0]['passwd']=='yes'
    assert json.loads(outputs['inv_usb'])[0]['port']=='1-2'
    assert json.loads(outputs['inv_coral'])[0]['status']=='ALIVE'


def test_mapping_php_active_xml_projection_and_quoted_vm_name(tmp_path):
    import os
    import subprocess
    import shlex
    fake=tmp_path/'virsh'
    fake.write_text('''#!/bin/sh
if [ "$1" = list ]; then printf '%s\\n' 'VM $(touch SHOULD_NOT_EXIST)'; exit 0; fi
if [ "$1" = dumpxml ] && [ "$3" = 'VM $(touch SHOULD_NOT_EXIST)' ]; then
printf '%s' '<domain><name>secret</name><devices><hostdev type="usb"><source><vendor id="0x1234"/><product id="0xabcd"/><address bus="1" device="4"/></source></hostdev><disk><serial>PRIVATE</serial></disk></devices></domain>'
exit 0
fi
exit 9
''')
    fake.chmod(0o755)
    env=dict(os.environ,PATH=str(tmp_path)+':'+os.environ['PATH'])
    command=dict(inventory_sections())['inv_usb_mapping']
    output=subprocess.check_output(shlex.split(command),text=True,env=env,cwd=tmp_path)
    row=json.loads(output)
    assert row['active']==[dict(vm='VM $(touch SHOULD_NOT_EXIST)',vendor='1234',product='abcd',bus='1',dev='4')]
    assert not (tmp_path/'SHOULD_NOT_EXIST').exists()
    assert 'PRIVATE' not in output and '<domain>' not in output


def test_mapping_php_keeps_paused_active_vm_and_excludes_stopped_vm(tmp_path):
    import os
    import subprocess
    import shlex
    fake=tmp_path/'virsh'
    # Model libvirt's active default and the state/all/inactive filters.
    fake.write_text('''#!/bin/sh
if [ "$1" = list ]; then
    case " $* " in
        *" --inactive "*) printf '%s\\n' 'Stopped VM';;
        *" --all "*) printf '%s\\n' 'Running VM' 'Paused VM' 'Stopped VM';;
        *" --state-running "*) printf '%s\\n' 'Running VM';;
        *) printf '%s\\n' 'Running VM' 'Paused VM';;
    esac
    exit 0
fi
if [ "$1" = dumpxml ] && [ "$2" = --domain ] && [ "$#" = 3 ]; then
    case "$3" in
        'Running VM') dev=4;;
        'Paused VM') dev=5;;
        'Stopped VM') dev=6;;
        *) exit 9;;
    esac
    printf '<domain><devices><hostdev type="usb"><source><address bus="1" device="%s"/></source></hostdev></devices></domain>' "$dev"
    exit 0
fi
exit 9
''')
    fake.chmod(0o755)
    env=dict(os.environ,PATH=str(tmp_path)+':'+os.environ['PATH'])
    command=dict(inventory_sections())['inv_usb_mapping']
    output=subprocess.check_output(shlex.split(command),text=True,env=env,cwd=tmp_path)
    assert json.loads(output)['active']==[
        dict(vm='Running VM',bus='1',dev='4'),
        dict(vm='Paused VM',bus='1',dev='5'),
    ]
    result=parse([device(),device('1-3','1','5'),device('1-4','1','6')],json.loads(output))
    assert [(row.state,row.assigned_vm) for row in result.usb]==[
        ('vm','Running VM'),('vm','Paused VM'),('host',None),
    ]
    assert 'inv_usb_mapping' not in result.failed


@pytest.mark.parametrize('bad',[{'vm':'VM','vendor':None,'product':'abcd'},{'vm':None,'bus':'1','dev':'4'},{'vm':'VM','bus':{},'dev':'4'}])
def test_invalid_optional_mapping_is_partial_but_usb_survives(bad):
    result=parse([device()],mapping(bad))
    assert result.usb is not None and len(result.usb)==1
    assert result.mapping_status=='partial' and 'inv_usb_mapping' in result.failed


def test_stopped_vm_configuration_has_no_active_assignment():
    result=parse([device()],dict(supported=True,active=[],configured=[dict(vm='Stopped VM',bus='1',dev='4')]))
    assert result.usb[0].state=='host' and result.usb[0].assigned_vm is None


@pytest.mark.parametrize('rows',[[1],[{}],[dict(port='not-a-sysfs-port')]])
def test_malformed_usb_source_is_unavailable_not_zero(rows):
    result=parse(rows,mapping())
    assert result.usb is None and 'inv_usb' in result.failed
