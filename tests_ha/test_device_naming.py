"""Device names, grouping and late container discovery in Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.unraid_ssh.coordinator import UpdateState
from custom_components.unraid_ssh.entity import (
    device_for_container,
    disk_device,
    loose_containers_device,
    share_device,
    stack_device,
    vm_device,
)
from custom_components.unraid_ssh.model import ImageStatus, Snapshot, Stack
from custom_components.unraid_ssh.parse import Container, Disk, Gpu, Share, Vm
from custom_components.unraid_ssh.sensor import _build as build_sensors
from custom_components.unraid_ssh.switch import _build as build_switches
from custom_components.unraid_ssh.update import async_setup_entry as setup_updates


def _snapshot(
    *,
    containers: tuple[Container, ...] = (),
    stacks: tuple[Stack, ...] = (),
    loose: tuple[Container, ...] = (),
    shares: tuple[Share, ...] = (),
    gpus: tuple[Gpu, ...] = (),
) -> Snapshot:
    return Snapshot(
        array=None,
        disks=(),
        shares=shares,
        cpu_times=None,
        cpu_percent=None,
        memory=None,
        load=None,
        gpus=gpus,
        stacks=stacks,
        template_containers=loose,
        containers=containers,
        vms=(),
        failed=frozenset(),
    )


@dataclass
class _Entry:
    entry_id: str = "entry-1"
    title: str = "My Unraid"
    runtime_data: Any = field(default_factory=lambda: SimpleNamespace(icons=None))
    unload_callbacks: list[Any] = field(default_factory=list)

    def async_on_unload(self, callback):
        self.unload_callbacks.append(callback)


@dataclass
class _Coordinator:
    entry: _Entry
    data: Any
    listeners: list[Any] = field(default_factory=list)
    last_update_success: bool = True
    host: str = "example.invalid"
    client: Any = None

    def async_add_listener(self, callback):
        self.listeners.append(callback)
        return lambda: self.listeners.remove(callback)


def test_child_devices_use_translatable_type_names_and_stable_identifiers():
    entry = _Entry()
    coordinator = _Coordinator(entry, _snapshot())
    stack = Stack("live_project", "live_project", "/stacks/My Stack/", True, True, 1, (), ())
    disk = Disk("disk1", "sdb", "Data", "ok", 30, False, 100, 50, 50.0, 0, "Mounted")
    vm = Vm("Windows", "running")
    share = Share("Media Backup", 10, 20)

    cases = (
        (disk_device(coordinator, disk), "entry-1_disk_disk1", "disk", "disk1"),
        (stack_device(coordinator, stack), "entry-1_stack_My Stack", "stack", "My Stack"),
        (vm_device(coordinator, vm), "entry-1_vm_Windows", "vm", "Windows"),
        (share_device(coordinator, share), "entry-1_share_Media Backup", "share", "Media Backup"),
    )

    for info, identifier, translation_key, component in cases:
        assert info["identifiers"] == {("unraid_ssh", identifier)}
        assert "name" not in info
        assert info["translation_key"] == translation_key
        assert info["translation_placeholders"] == {
            "prefix": "My Unraid",
            "component": component,
        }


def test_switches_group_all_standalone_containers_on_one_translated_device():
    first = Container("alpha", "running", "example/a:latest", "", "")
    second = Container("beta", "exited", "example/b:latest", "", "")
    snapshot = _snapshot(containers=(first, second), loose=(first, second))
    coordinator = _Coordinator(_Entry(), snapshot)

    entities = build_switches(coordinator)(snapshot)

    assert {entity.unique_id for entity in entities.values()} == {
        "entry-1_container_alpha",
        "entry-1_container_beta",
    }
    for name in ("alpha", "beta"):
        entity = entities[f"container_{name}"]
        assert entity.device_info == loose_containers_device(coordinator)
        assert entity.translation_key == "container"
        assert entity.translation_placeholders == {"container": name}


def test_orphan_compose_container_stays_on_a_stack_device():
    container = Container("web", "running", "example/web:latest", "orphan", "web")
    snapshot = _snapshot(containers=(container,))
    coordinator = _Coordinator(_Entry(), snapshot)

    info = device_for_container(coordinator, container)

    assert info["identifiers"] == {("unraid_ssh", "entry-1_stack_orphan")}
    assert info["translation_key"] == "stack"
    assert info["translation_placeholders"]["component"] == "orphan"


def test_share_and_gpu_sensors_move_to_their_specified_devices_without_new_ids():
    share = Share("Media", 10, 20)
    gpu = Gpu(0, "Example GPU", 1.0, 2, 4, 50.0, 30, 20.0)
    snapshot = _snapshot(shares=(share,), gpus=(gpu,))
    coordinator = _Coordinator(_Entry(), snapshot)

    entities = build_sensors(coordinator)(snapshot)

    share_used = entities["share_used_Media"]
    assert share_used.unique_id == "entry-1_share_used_Media"
    assert share_used.device_info == share_device(coordinator, share)
    assert share_used.translation_placeholders == {}

    gpu_util = entities["gpu_util_0"]
    assert gpu_util.unique_id == "entry-1_gpu_util_0"
    assert gpu_util.device_info["identifiers"] == {("unraid_ssh", "entry-1")}
    assert gpu_util.translation_placeholders == {"gpu": "Example GPU", "index": "0"}


@pytest.mark.asyncio
async def test_update_waits_for_fast_snapshot_then_registers_from_fast_listener():
    entry = _Entry()
    fast = _Coordinator(entry, _snapshot())
    updates = _Coordinator(
        entry,
        UpdateState(
            images={"alpha": ImageStatus("alpha", "example/a:latest", "sha256:old", "sha256:new", True)},
            checked_at=None,
        ),
    )
    entry.runtime_data = SimpleNamespace(coordinator=fast, updates=updates, icons=None)
    added = []

    await setup_updates(None, entry, added.extend)

    assert added == []
    container = Container("alpha", "running", "example/a:latest", "", "")
    fast.data = _snapshot(containers=(container,), loose=(container,))
    for listener in tuple(fast.listeners):
        listener()

    assert len(added) == 1
    entity = added[0]
    assert entity.unique_id == "entry-1_update_alpha"
    assert entity.device_info == loose_containers_device(fast)
    assert entity.translation_key == "container_update"
    assert entity.translation_placeholders == {"container": "alpha"}
