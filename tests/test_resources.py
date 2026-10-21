"""Resource measurements must never invent guest usage or erase inventory."""
from dataclasses import replace
import pytest
from unraid_ssh import actions, collect, model, parse


def test_docker_units_and_invalid_rows_are_isolated():
    rows = '\n'.join(['{"Name":"web","CPUPerc":"150.5%","MemUsage":"1.5MiB / 2GB"}', '{broken', '{"Name":"bad","CPUPerc":"NaN%","MemUsage":"-2MiB / 3GiB"}'])
    samples = parse.parse_docker_stats(rows)
    assert samples['web'].cpu_percent == 150.5
    assert samples['web'].ram_used == 1572864
    assert samples['web'].ram_limit is None
    assert samples['web'].ram_capacity == 2000000000
    assert samples['bad'].cpu_percent is None
    assert samples['bad'].ram_used is None


def stats(cpu=1000000000, extra=''):
    return f"Domain: 'Guest'\n  cpu.time={cpu}\n  balloon.current=1048576\n  balloon.maximum=2097152\n  balloon.rss=1200000\n{extra}"


def inventory(**extra):
    return {'docker': 'web\trunning\texample/web\t\t\t\t\tdockerman', 'vms': ' Id Name State\n----------------\n 1  Guest  running\n', **extra}


def test_resource_inventory_binding_and_vm_cpu_delta():
    first = model.build_snapshot(inventory(docker_stats='{"Name":"ghost","CPUPerc":"10%"}', vm_stats=stats()), None, sampled_at=10)
    assert first.container_resources == {}
    assert first.vm_resources['Guest'].cpu_percent is None
    second = model.build_snapshot(inventory(vm_stats=stats(4000000000)), first, sampled_at=12)
    assert second.vm_resources['Guest'].cpu_percent == 150
    assert second.vm_resources['Guest'].ram_allocated == 1073741824
    assert second.vm_resources['Guest'].ram_host_rss == 1228800000
    assert second.vm_resources['Guest'].ram_used is None
    reset = model.build_snapshot(inventory(vm_stats=stats(1)), second, sampled_at=14)
    assert reset.vm_resources['Guest'].cpu_percent is None
    stopped = model.build_snapshot({**inventory(vm_stats=stats(5)), 'vms': ' -  Guest  shut off\n'}, reset, sampled_at=16)
    assert stopped.vm_resources == {}
    restarted = model.build_snapshot(inventory(vm_stats=stats(1000)), stopped, sampled_at=18)
    assert restarted.vm_resources['Guest'].cpu_percent is None
    failed = model.build_snapshot(inventory(), restarted, sampled_at=20)
    assert len(failed.vms) == 1 and len(failed.containers) == 1
    assert {'docker_stats', 'vm_stats'} <= failed.failed


def test_guest_ram_requires_fresh_valid_guest_stats(monkeypatch):
    monkeypatch.setattr(parse.time, 'time', lambda: 1000)
    sample = parse.parse_vm_stats(stats(extra='  balloon.available=1000000\n  balloon.unused=250000\n  balloon.last-update=990\n'))['Guest']
    assert sample.ram_used == 768000000
    assert parse.parse_vm_stats(stats(extra='  balloon.available=1000000\n  balloon.unused=250000\n  balloon.last-update=1\n'))['Guest'].ram_used is None
    assert parse.parse_vm_stats(stats(extra='  balloon.available=100\n  balloon.unused=200\n  balloon.last-update=990\n'))['Guest'].ram_used is None


def test_collect_has_independent_bounded_resource_sections():
    commands = dict(collect.SECTIONS)
    assert 'docker_stats' in commands and 'vm_stats' in commands
    assert 'timeout' in commands['docker_stats'] and 'timeout' in commands['vm_stats']
    assert '--cpu-total' in commands['vm_stats'] and '--balloon' in commands['vm_stats']


def test_vm_restart_is_one_native_quoted_command():
    assert actions.vm_restart_cmd("Guest's VM; echo nope") == "virsh reboot 'Guest'\"'\"'s VM; echo nope'"


def test_unlimited_docker_stats_host_capacity_is_not_a_container_limit():
    raw = '{"Name":"web","CPUPerc":"1%","MemUsage":"10MiB / 32GiB"}'
    unlimited = model.build_snapshot(inventory(docker_stats=raw, docker_limits='/web\t0\n'), None)
    assert unlimited.container_resources['web'].ram_limit is None
    limited = model.build_snapshot(inventory(docker_stats=raw, docker_limits='/web\t33554432\n'), None)
    assert limited.container_resources['web'].ram_limit == 33554432
    failed = model.build_snapshot(inventory(docker_stats=raw), None)
    assert failed.container_resources['web'].ram_limit is None
    assert failed.container_resources['web'].ram_used == 10485760


def test_limit_reader_handles_multiple_ids_and_only_reads_memory(tmp_path):
    import os
    import subprocess
    docker = tmp_path / 'docker'
    docker.write_text("#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ['ps','-q']: print('abc\\ndef')\nelif sys.argv[1:] == ['inspect','--format','{{.Name}}{{\"\\\\t\"}}{{.HostConfig.Memory}}','abc','def']: print('/web\\t4096')\nelse: sys.exit(9)\n")
    docker.chmod(0o755)
    done = subprocess.run(['bash', '-c', dict(collect.SECTIONS)['docker_limits']], env={**os.environ, 'PATH':str(tmp_path)+':'+os.environ['PATH']}, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert parse.parse_docker_limits(done.stdout) == {'web':4096}


def test_vm_new_domain_runtime_does_not_reuse_previous_cpu_counter():
    before = model.build_snapshot(inventory(vm_stats=stats()), None, sampled_at=1)
    changed = {**inventory(vm_stats=stats(5000000000)), 'vms':' 2  Guest  running\n'}
    after = model.build_snapshot(changed, before, sampled_at=3)
    assert after.vm_resources['Guest'].cpu_percent is None
    assert after.vms[0].name == 'Guest'
