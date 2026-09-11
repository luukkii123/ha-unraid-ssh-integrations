"""Stable device grouping for containers and shares."""

from __future__ import annotations

from unraid_ssh import collect
from unraid_ssh.devices import container_device_suffix, share_device_suffix
from unraid_ssh.model import build_snapshot, stack_key
from unraid_ssh.parse import Container


def test_all_standalone_containers_share_one_suffix():
    snapshot = build_snapshot({}, None)
    first = Container("alpha", "running", "example/a:latest", "", "")
    second = Container("beta", "exited", "example/b:latest", "", "")

    assert container_device_suffix(snapshot, first) == "containers"
    assert container_device_suffix(snapshot, second) == "containers"


def test_orphan_compose_project_is_not_a_standalone_container():
    snapshot = build_snapshot({}, None)
    container = Container("alpha", "running", "example/a:latest", "orphan", "web")

    assert container_device_suffix(snapshot, container) == "stack_orphan"


def test_stack_containers_use_stable_stack_key_from_recorded_snapshot(fixture):
    snapshot = build_snapshot(collect.split_output(fixture("full_output.txt")), None)

    for stack in snapshot.stacks:
        for container in stack.containers:
            assert container_device_suffix(snapshot, container) == f"stack_{stack_key(stack)}"

    gps = next(stack for stack in snapshot.stacks if stack_key(stack) == "gps-bridge")
    assert gps.name == "gps_bridge"
    assert gps.containers


def test_share_devices_keep_distinct_complete_names():
    assert share_device_suffix("Media") == "share_Media"
    assert share_device_suffix("media") == "share_media"
    assert share_device_suffix("Media_Backup") == "share_Media_Backup"
    assert share_device_suffix("Media Backup") == "share_Media Backup"
