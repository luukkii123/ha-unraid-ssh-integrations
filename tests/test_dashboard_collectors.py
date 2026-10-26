"""Execute fixed PHP readers with invented files and fake local tools."""
import json
from pathlib import Path
import os
import shlex
import subprocess
import pytest
from unraid_ssh import collect,collect_telemetry


def execute(command,tmp_path,files,tools=None):
    argv=shlex.split(command)
    for original,text in files.items():
        path=tmp_path / ('fixture-'+str(len(list(tmp_path.iterdir()))))
        path.write_text(text)
        argv[2]=argv[2].replace(original,str(path))
    env=os.environ.copy()
    if tools:
        bindir=tmp_path/'bin'; bindir.mkdir()
        for name,body in tools.items():
            path=bindir/name; path.write_text('#!/bin/sh\n'+body); path.chmod(0o755)
        env['PATH']=str(bindir)+':'+env['PATH']
    return subprocess.run(argv,text=True,capture_output=True,env=env,timeout=10)


@pytest.mark.parametrize('missing,want',[(False,'online'),(True,'degraded')])
def test_pool_device_count_metadata_is_not_a_fake_member_and_carrier_wins(tmp_path,missing,want):
    disks='["pool-member"]\nname="pool-member"\ntype="Cache"\nfsType="btrfs"\nfsMountpoint="/mnt/example-pool"\nfsStatus="Mounted"\n["example-pool"]\nname="example-pool"\ntype="Cache"\nfsType="btrfs"\nfsMountpoint="/mnt/example-pool"\nfsStatus="Mounted"\ndevices="2"\nfsSize="100"\nfsFree="25"'
    show='devid 1 size 100 path /dev/sda\n'+('Some devices missing\n' if missing else 'devid 2 size 100 path /dev/sdb\n')
    body='case "$1" in filesystem) printf %s '+shlex.quote(show)+';; stats) exit 0;; device) exit 0;; esac'
    result=execute(collect_telemetry._POOLS,tmp_path,{'/var/local/emhttp/disks.ini':disks},{'btrfs':body})
    assert result.returncode==0,result.stderr
    pools=json.loads(result.stdout)
    assert len(pools)==1
    assert pools[0]['name']=='example-pool'
    assert pools[0]['total_bytes']==102400
    assert pools[0]['used_bytes']==76800
    assert pools[0]['state']==want
    assert '2' not in pools[0]['members']
    assert all('/' not in member for member in pools[0]['members'])


def test_share_permissions_reader_drops_users_and_unknown_shares(tmp_path):
    result=execute(collect_telemetry._SHARES,tmp_path,{'/var/local/emhttp/shares.ini':'["Example"]\nname="Example"','/var/local/emhttp/sec.ini':'["Example"]\nexport="yes"\nsecurity="private"\nreadList="SECRET"\nwriteList="SECRET"\n["Unlisted"]\nsecurity="public"'})
    assert result.returncode==0
    assert 'private' in result.stdout
    assert 'SECRET' not in result.stdout
    assert 'Unlisted' not in result.stdout


def test_streams_count_unique_paths_without_exporting_them_and_fail_closed(tmp_path):
    files={'/var/local/emhttp/shares.ini':'["Example"]\nname="Example"','/var/local/emhttp/disks.ini':'["disk1"]\ntype="Data"\nfsStatus="Mounted"\nfsMountpoint="/mnt/example-disk"'}
    result=execute(collect_telemetry._STREAMS,tmp_path,files,{'lsof':"printf 'n/mnt/example-disk/Example/secret-a\\nn/mnt/example-disk/Example/secret-a\\nn/mnt/example-disk/Example/secret-b\\n'"})
    assert result.returncode==0
    assert json.loads(result.stdout)=={'Example':2}
    assert 'secret' not in result.stdout


def test_collector_deadline_keeps_partial_failures_and_final_marker(monkeypatch):
    monkeypatch.setattr(collect,'SECTIONS',(('var',"printf 'ok'"),('disks','false')))
    monkeypatch.setattr(collect,'dashboard_sections',lambda **kwargs:(('dash_requested',"printf 'dash_cpu'"),('dash_cpu',"printf 'sample'")))
    result=subprocess.run(['bash','-c',collect.build_state_command()],capture_output=True,text=True,check=True,timeout=5)
    assert collect.split_output(result.stdout)=={'var':'ok','dash_requested':'dash_cpu','dash_cpu':'sample'}
    assert result.stdout.count('@@@ end')==1


def test_pool_member_alias_never_exports_serial_number(tmp_path):
    disks='["example-pool"]\nname="example-pool"\ntype="Cache"\nfsType="btrfs"\nfsMountpoint="/mnt/example-pool"\nfsStatus="Mounted"\ndevices="1"\nfsSize="100"\nfsFree="25"'
    result=execute(collect_telemetry._POOLS,tmp_path,{'/var/local/emhttp/disks.ini':disks},{'btrfs':"case \"$1\" in filesystem) printf 'devid 1 size 100 path /dev/disk/by-id/ata-SECRET_SERIAL_123\\n';; device) exit 0;; esac"})
    assert result.returncode==0
    assert 'SECRET' not in result.stdout
