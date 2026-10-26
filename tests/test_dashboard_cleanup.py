"""Real nested command shapes must stay inside their managed worker group."""

import os
import json
from pathlib import Path
import shlex
import signal
import subprocess
import time

import pytest

from unraid_ssh import collect, collect_telemetry


def running(pid):
    stat = Path(f'/proc/{pid}/stat')
    return stat.exists() and stat.read_text().split()[2] != 'Z'


def exercise_cleanup(monkeypatch, tmp_path, name, command, tools, *, cancel, ignore_term):
    """Replace only external tools; execute the actual builder and PHP bodies."""
    records = tmp_path / 'children'
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    output = tmp_path / 'outputs'
    output.mkdir()
    hang = (
        ("trap '' TERM\n" if ignore_term else '')
        + 'sleep 30 &\nchild=$!\n'
        + 'printf "%s %s\\n" "$$" "$child" > '+shlex.quote(str(records))+'\nwait\n'
    )
    for tool, body in tools.items():
        path = bindir / tool
        path.write_text('#!/bin/sh\n'+body.replace('HANG', hang))
        path.chmod(0o755)
    monkeypatch.setattr(collect, 'SECTIONS', (('var', 'printf core-ok'),))
    monkeypatch.setattr(collect, 'dashboard_sections', lambda **kwargs: (
        ('dash_requested', 'printf '+name), (name, command),
    ))
    script = collect.build_state_command(include_inventory=True, include_streams=True)
    script = script.replace('mktemp -d', 'mktemp -d -p '+shlex.quote(str(output)))
    # A process outside the worker group must survive its cleanup signals.
    unrelated = subprocess.Popen(['sleep', '30'])
    process = subprocess.Popen(['/bin/sh', '-c', script], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True,
                               env={**os.environ, 'PATH': str(bindir)+':'+os.environ['PATH']})
    pids = []
    started = time.monotonic()
    try:
        deadline = started+3
        while time.monotonic() < deadline:
            if records.exists():
                pids = [int(value) for value in records.read_text().split()]
                if len(pids) == 2:
                    break
            time.sleep(0.01)
        assert len(pids) == 2, 'nested tool never started'
        assert all(running(pid) for pid in pids)
        if cancel:
            process.terminate()
        stdout, stderr = process.communicate(timeout=6)
        # Allow reaping to finish; a surviving TERM-ignoring child sleeps 30s.
        time.sleep(0.05)
        active = [pid for pid in pids if running(pid)]
        temporary = list(output.iterdir())
        print(f'{name} cancel={cancel} ignore_term={ignore_term} '
              f'elapsed={time.monotonic()-started:.2f} exit={process.returncode} '
              f'active={active} temporary={len(temporary)} unrelated={unrelated.poll()}')
        assert temporary == []
        assert unrelated.poll() is None
        assert active == [], 'nested tool or its child escaped worker cleanup'
        assert process.returncode == (143 if cancel else 0)
        if not cancel:
            sections = collect.split_output(stdout)
            assert sections['var'] == 'core-ok'
            assert name not in sections
            assert stdout.count('@@@ end') == 1
            assert f'{name} status=' in stdout
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
        # Target only the synthetic tool PIDs recorded by this test.
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        unrelated.terminate()
        unrelated.wait(timeout=2)


def fixture_php(command, tmp_path, files):
    argv = shlex.split(command)
    for index, (original, text) in enumerate(files.items()):
        path = tmp_path / f'fixture-{index}.ini'
        path.write_text(text)
        argv[2] = argv[2].replace(original, str(path))
    return ' '.join(shlex.quote(value) for value in argv)


@pytest.mark.parametrize('cancel', [False, True], ids=['deadline', 'builder-term'])
@pytest.mark.parametrize('ignore_term', [False, True], ids=['ordinary', 'ignore-term'])
def test_real_stream_php_cleans_nested_lsof_and_sleep(monkeypatch, tmp_path, cancel, ignore_term):
    command = fixture_php(collect_telemetry._STREAMS, tmp_path, {
        '/var/local/emhttp/shares.ini': '["Example"]\nname="Example"',
        '/var/local/emhttp/disks.ini': '["disk1"]\ntype="Data"\nfsStatus="Mounted"\nfsMountpoint="/mnt/example-disk"',
    })
    exercise_cleanup(monkeypatch, tmp_path, 'dash_streams', command, {'lsof': 'HANG'},
                     cancel=cancel, ignore_term=ignore_term)


@pytest.mark.parametrize('cancel', [False, True], ids=['deadline', 'builder-term'])
@pytest.mark.parametrize('probe', ['btrfs-show', 'btrfs-stats', 'zpool-list', 'zpool-status'])
def test_real_pool_php_cleans_each_nested_probe(monkeypatch, tmp_path, cancel, probe):
    filesystem = 'btrfs' if probe.startswith('btrfs') else 'zfs'
    command = fixture_php(collect_telemetry._POOLS, tmp_path, {
        '/var/local/emhttp/disks.ini': '["example-pool"]\nname="example-pool"\ntype="Cache"\n'
        f'fsType="{filesystem}"\nfsStatus="Mounted"\nfsMountpoint="/mnt/example-pool"',
    })
    bodies = {
        'btrfs-show': 'HANG',
        'btrfs-stats': 'if [ "$1" = filesystem ]; then exit 0; fi\nHANG',
        'zpool-list': 'HANG',
        'zpool-status': 'if [ "$1" = list ]; then printf "example-pool ONLINE\\n"; exit 0; fi\nHANG',
    }
    tool = 'btrfs' if filesystem == 'btrfs' else 'zpool'
    exercise_cleanup(monkeypatch, tmp_path, 'dash_pools', command, {tool: bodies[probe]},
                     cancel=cancel, ignore_term=True)


@pytest.mark.parametrize('name,tool', [('docker_stats', 'docker'), ('docker_limits', 'docker'), ('vm_stats', 'virsh')])
def test_fixed_nested_shell_timeouts_clean_on_builder_term(monkeypatch, tmp_path, name, tool):
    command = dict(collect.SECTIONS)[name]
    exercise_cleanup(monkeypatch, tmp_path, name, command, {tool: 'HANG'},
                     cancel=True, ignore_term=True)


def test_worker_slot_is_released_only_after_ignoring_nested_probe_dies(monkeypatch, tmp_path):
    state = tmp_path / 'workers.json'
    state.write_text('{"pids":[],"maximum":0}')
    program = '''import fcntl,json,os,signal,sys,time
from pathlib import Path
signal.signal(signal.SIGTERM,signal.SIG_IGN)
def running(pid):
    path=Path(f'/proc/{pid}/stat')
    return path.exists() and path.read_text().split()[2]!='Z'
with open(sys.argv[1],'r+') as handle:
    fcntl.flock(handle,fcntl.LOCK_EX)
    data=json.load(handle)
    data['pids'].append(os.getpid())
    data['maximum']=max(data['maximum'],sum(running(pid) for pid in data['pids']))
    handle.seek(0);json.dump(data,handle);handle.truncate()
time.sleep(30)
'''
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    lsof = bindir / 'lsof'
    lsof.write_text('#!/bin/sh\nexec python -c '+shlex.quote(program)+' '+shlex.quote(str(state)))
    lsof.chmod(0o755)
    command = fixture_php(collect_telemetry._STREAMS, tmp_path, {
        '/var/local/emhttp/shares.ini': '["Example"]\nname="Example"',
        '/var/local/emhttp/disks.ini': '["disk1"]\ntype="Data"\nfsStatus="Mounted"\nfsMountpoint="/mnt/example-disk"',
    })
    names = ('alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta',
             'eta', 'theta', 'iota', 'kappa', 'lambda', 'mu')
    monkeypatch.setattr(collect, 'SECTIONS', tuple((name, command) for name in names))
    monkeypatch.setattr(collect, 'dashboard_sections', lambda **kwargs: (('dash_requested', 'printf marker'),))
    try:
        result = subprocess.run(['/bin/sh', '-c', collect.build_state_command()],
                                capture_output=True, text=True, timeout=8,
                                env={**os.environ, 'PATH': str(bindir)+':'+os.environ['PATH']})
        data = json.loads(state.read_text())
        print(f'nested probe maximum={data["maximum"]} started={len(data["pids"])}')
        assert result.returncode == 0
        assert len(data['pids']) == 12
        assert 2 <= data['maximum'] <= 4
        assert all(not running(pid) for pid in data['pids'])
        assert collect.split_output(result.stdout) == {'dash_requested': 'marker'}
    finally:
        for pid in json.loads(state.read_text())['pids']:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
