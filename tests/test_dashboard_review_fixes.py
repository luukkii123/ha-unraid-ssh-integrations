"""Synthetic regressions for the independent stage-two backend review."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import time

import pytest

from unraid_ssh import collect, collect_telemetry, telemetry
from unraid_ssh.model import build_snapshot
from unraid_ssh.server_actions import ServerAction, ServerActionError, server_action_command
from test_dashboard_collectors import execute
from test_dashboard_telemetry import sample


START = '\n'.join(f'{key}="{value}"' for key, value in {
    'mdState': 'STOPPED', 'fsState': 'Stopped', 'configValid': 'yes',
    'startMode': 'Normal', 'mdNumMissing': '0', 'mdResync': '0',
    'mdResyncPos': '0', 'mdResyncAction': 'check', 'mdResyncCorr': '0',
}.items())


def slot(name, *, device='sda', status='DISK_OK', mount='/mnt/example-pool', count='1'):
    return '\n'.join((f'["{name}"]', f'name="{name}"', 'type="Cache"',
                      f'device="{device}"', f'status="{status}"',
                      f'fsMountpoint="{mount}"', f'devices="{count}"'))


@pytest.mark.parametrize('disks,pools', [
    (slot('example-pool', device='', status='DISK_NP_MISSING'), []),
    (slot('example-pool')+'\n'+slot('other-pool', device='sdb', mount='', count='2'),
     [{'name': 'example-pool', 'state': 'online', 'members': ['example-pool']}]),
])
def test_array_start_rejects_every_unobserved_or_missing_configured_pool(disks, pools):
    snapshot = build_snapshot({'var': START, 'disks': disks, 'dash_pools': json.dumps(pools)}, None, sampled_at=1)
    # The existing entity parser intentionally still skips empty devices.
    assert len(snapshot.disks) == (0 if 'DISK_NP_MISSING' in disks else 2)
    with pytest.raises(ServerActionError, match='array_start_blocked'):
        server_action_command(ServerAction.ARRAY_START, snapshot)


def test_array_start_accepts_complete_pool_inventory_and_rejects_partial_members():
    disks = slot('example-pool', count='2')+'\n'+slot('example-member', device='sdb', count='2')
    pools = [{'name': 'example-pool', 'state': 'online', 'members': ['example-pool', 'example-member']}]
    sections = {'var': START, 'disks': disks, 'dash_pools': json.dumps(pools)}
    snapshot = build_snapshot(sections, None, sampled_at=1)
    assert 'cmdStart=Start' in server_action_command(ServerAction.ARRAY_START, snapshot)
    pools[0]['members'].pop()
    snapshot = build_snapshot({**sections, 'dash_pools': json.dumps(pools)}, snapshot, sampled_at=2)
    with pytest.raises(ServerActionError, match='array_start_blocked'):
        server_action_command(ServerAction.ARRAY_START, snapshot)


@pytest.mark.parametrize('change', [
    {'disks': slot('example-pool', status='unexpected')},
    {'disks': slot('example-pool', count='unknown')},
    {'disks': '[broken'},
    {'dash_pools': '[]'},
    {'dash_pools': None},
])
def test_pool_safety_is_unknown_for_incomplete_fresh_evidence(change):
    sections = {'var': START, 'disks': slot('example-pool'),
                'dash_pools': '[{"name":"example-pool","state":"online","members":["example-pool"]}]'}
    first = build_snapshot(sections, None, sampled_at=1)
    sections.update(change)
    if sections.get('dash_pools') is None:
        sections.pop('dash_pools')
    current = build_snapshot(sections, first, sampled_at=2)
    with pytest.raises(ServerActionError, match='array_start_blocked'):
        server_action_command(ServerAction.ARRAY_START, current)


def test_optional_gpu_timeout_preserves_following_inventory_and_normal_stats(monkeypatch):
    monkeypatch.setattr(collect, 'SECTIONS', (
        ('var', 'printf core-ok'), ('gpu', 'sleep 9'),
        ('docker', 'printf docker-ok'), ('vms', 'printf vm-ok'),
        ('docker_stats', 'sleep 2; printf stats-ok'),
    ))
    monkeypatch.setattr(collect, 'dashboard_sections', lambda **kwargs: (
        ('dash_requested', 'printf dash_cpu'), ('dash_cpu', 'printf cpu-ok'),
    ))
    started = time.monotonic()
    result = subprocess.run(['bash', '-c', collect.build_state_command()],
                            capture_output=True, text=True, check=True, timeout=12)
    sections = collect.split_output(result.stdout)
    assert sections['docker'] == 'docker-ok'
    assert sections['vms'] == 'vm-ok'
    assert sections['dash_cpu'] == 'cpu-ok'
    assert sections['docker_stats'] == 'stats-ok'
    assert 'gpu' not in sections
    # The worker stays alive until its group is killed, including descendants.
    assert 'gpu status=137' in result.stdout
    assert result.stdout.count('@@@ end') == 1
    assert time.monotonic()-started < 8.5


def test_parallel_workers_keep_sections_ordered_and_limit_concurrency(monkeypatch, tmp_path):
    state = tmp_path / 'workers.json'
    state.write_text('[0,0]')
    program = '''import fcntl,json,sys,time
path=sys.argv[1]
def update(delta):
    with open(path,'r+') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        active,maximum=json.load(handle)
        active+=delta
        handle.seek(0);json.dump([active,max(active,maximum)],handle);handle.truncate()
update(1)
print(sys.argv[2],end='',flush=True)
time.sleep(0.05)
print('-end',end='')
update(-1)
'''
    names = ('alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta', 'theta', 'iota')
    monkeypatch.setattr(collect, 'SECTIONS', tuple((name, 'python -c '+shlex.quote(program)+' '+shlex.quote(str(state))+' '+name) for name in names))
    monkeypatch.setattr(collect, 'dashboard_sections', lambda **kwargs: (('dash_requested', 'printf marker'),))
    result = subprocess.run(['/bin/sh', '-c', collect.build_state_command()], capture_output=True, text=True, check=True, timeout=5)
    assert list(collect.split_output(result.stdout)) == ['dash_requested', *names]
    assert collect.split_output(result.stdout) == {'dash_requested': 'marker', **{name: name+'-end' for name in names}}
    active, maximum = json.loads(state.read_text())
    assert active == 0
    assert 2 <= maximum <= 4


def _running(pid):
    stat = Path(f'/proc/{pid}/stat')
    return stat.exists() and stat.read_text().split()[2] != 'Z'


@pytest.mark.parametrize('cancel', [False, True])
def test_timeout_and_cancellation_clean_children_and_temporary_outputs(monkeypatch, tmp_path, cancel):
    pidfile = tmp_path / 'child.pid'
    output = tmp_path / 'outputs'
    output.mkdir()
    command = "trap '' TERM; sleep 9 & echo $! > "+shlex.quote(str(pidfile))+"; wait"
    monkeypatch.setattr(collect, 'SECTIONS', (('gpu', command),))
    monkeypatch.setattr(collect, 'dashboard_sections', lambda **kwargs: (('dash_requested', 'printf marker'),))
    script = collect.build_state_command().replace('mktemp -d', 'mktemp -d -p '+shlex.quote(str(output)))
    process = subprocess.Popen(['/bin/sh', '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic()+3
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pidfile.exists()
        pid = int(pidfile.read_text())
        if cancel:
            process.terminate()
        stdout, stderr = process.communicate(timeout=4)
        assert not _running(pid)
        assert list(output.iterdir()) == []
        if not cancel:
            assert process.returncode == 0
            assert 'gpu' not in collect.split_output(stdout)
            assert stdout.rstrip().endswith('@@@ end')
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.parametrize('raw,want', [('DOCKER_IMAGE_TYPE=""', 'image'),
                                      ('DOCKER_IMAGE_TYPE="xfs"', 'image'),
                                      ('DOCKER_IMAGE_TYPE="folder"', 'folder'),
                                      ('', 'unknown'), ('DOCKER_IMAGE_TYPE="image"', 'unknown')])
@pytest.mark.parametrize('mounted', [False, True])
def test_actual_docker_configuration_values_need_an_exact_mount(raw, want, mounted):
    data = telemetry.parse_dashboard_sections(sample(dash_docker=raw,
        dash_mounts='1 0 1:1 / /var/lib/docker rw - btrfs /dev/loop0 rw' if mounted else ''),
        None, sampled_at=1)
    assert data.docker_mode == want
    assert tuple(row['kind'] for row in data.storage) == (('docker_vdisk',) if want == 'image' and mounted else ())


def test_stream_counts_survive_three_skips_fail_closed_and_recover():
    data = telemetry.parse_dashboard_sections(sample(dash_streams='{"Example":7}'), None, sampled_at=0)
    skipped = sample()
    skipped.pop('dash_streams')
    skipped.pop('dash_shares')
    for now in (10, 20, 30):
        data = telemetry.parse_dashboard_sections(skipped, data, sampled_at=now)
        assert data.shares[0]['streams'] == 7
        assert data.sample_times['dash_streams'] == 0
        assert 'dash_streams' not in data.failed
    data = telemetry.parse_dashboard_sections({**skipped, 'dash_requested': 'dash_streams'}, data, sampled_at=60)
    assert data.shares[0]['streams'] is None
    for now in (70, 80, 90):
        data = telemetry.parse_dashboard_sections(skipped, data, sampled_at=now)
        assert data.shares[0]['streams'] is None
        assert 'dash_streams' in data.failed
        assert data.sample_times['dash_streams'] == 60
    data = telemetry.parse_dashboard_sections({**skipped, 'dash_streams': '{"Example":3}'}, data, sampled_at=120)
    assert data.shares[0]['streams'] == 3
    assert data.sample_times['dash_streams'] == 120
    assert 'dash_streams' not in data.failed


@pytest.mark.parametrize('name', ['Büro', 'Büro "Nord"', "Büro 'Süd' $()"])
@pytest.mark.parametrize('include_name', [False, True])
def test_real_php_share_metadata_round_trips_unicode_and_quotes(tmp_path, name, include_name):
    shares = '['+json.dumps(name, ensure_ascii=False)+']'
    if include_name:
        shares += '\nname='+json.dumps(name, ensure_ascii=False)
    sec = '['+json.dumps(name, ensure_ascii=False)+']\nexport="yes"\nsecurity="private"\nreadList="SECRET"\nwriteList="SECRET"\n["Unlisted"]\nsecurity="public"'
    result = execute(collect_telemetry._SHARES, tmp_path, {
        '/var/local/emhttp/shares.ini': shares, '/var/local/emhttp/sec.ini': sec,
    })
    assert result.returncode == 0, result.stderr
    data = telemetry.parse_dashboard_sections(sample(shares=shares, dash_shares=result.stdout), None, sampled_at=1)
    assert data.shares == ({'name': name, 'exported': True, 'security': 'private', 'streams': None},)
    assert 'SECRET' not in result.stdout
    assert 'Unlisted' not in result.stdout
