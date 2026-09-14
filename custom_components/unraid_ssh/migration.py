"""Reconcile owned registry assignments without changing entity identities."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DOMAIN
from .coordinator import UnraidConfigEntry
from .devices import container_assignment_complete
from .entity import (
    device_for_container,
    disk_device,
    loose_containers_device,
    server_device,
    share_device,
    stack_device,
    vm_device,
)


@callback
def async_reconcile_devices(hass: HomeAssistant, entry: UnraidConfigEntry) -> None:
    """Use successful snapshot sections only; this callback does no I/O."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    snapshot = coordinator.data
    if not coordinator.last_update_success or snapshot is None:
        return
    devices, entities = dr.async_get(hass), er.async_get(hass)
    own_devices = dr.async_entries_for_config_entry(devices, entry.entry_id)
    by_identifier = {identifier: device for device in own_devices for identifier in device.identifiers}

    def ensure(info: dr.DeviceInfo) -> str:
        identifier = next(iter(info["identifiers"]))
        device = by_identifier.get(identifier)
        if device is None or runtime.device_infos.get(identifier[1]) != info:
            device = devices.async_get_or_create(config_entry_id=entry.entry_id, **info)
            runtime.device_infos[identifier[1]] = dict(info)
            by_identifier[identifier] = device
        return device.id

    runtime.server_device_id = ensure(server_device(coordinator))
    targets: dict[tuple[str, str], str] = {}
    cleanup: set[str] = set()

    def target(domain: str, suffix: str, device_id: str) -> None:
        targets[(domain, entry.entry_id + "_" + suffix)] = device_id

    def owned_legacy(device: dr.DeviceEntry, suffix: str) -> bool:
        return (
            device.config_entries == {entry.entry_id}
            and device.identifiers == {(DOMAIN, entry.entry_id + "_" + suffix)}
            and not device.connections
        )

    if "docker" not in snapshot.failed:
        current = {container.name: container for container in snapshot.containers}
        for container in current.values():
            if not container_assignment_complete(snapshot, container):
                continue
            device_id = ensure(device_for_container(coordinator, container))
            target("switch", "container_" + container.name, device_id)
            target("update", "update_" + container.name, device_id)
        prefix = entry.entry_id + "_container_"
        for device in own_devices:
            for domain, identifier in device.identifiers:
                if domain != DOMAIN or not identifier.startswith(prefix):
                    continue
                name = identifier[len(prefix):]
                if not name or not owned_legacy(device, "container_" + name):
                    continue
                cleanup.add(device.id)
                # Exact legacy identifiers establish the former standalone
                # classification even when a temporary container has gone.
                if name not in current:
                    for entity in er.async_entries_for_device(entities, device.id, include_disabled_entities=True):
                        if entity.config_entry_id != entry.entry_id or entity.platform != DOMAIN:
                            continue
                        for entity_domain, suffix in (("switch", "container_" + name), ("update", "update_" + name)):
                            if entity.domain == entity_domain and entity.unique_id == entry.entry_id + "_" + suffix:
                                target(entity_domain, suffix, ensure(loose_containers_device(coordinator)))
        if not {"compose", "stacks"} & snapshot.failed:
            for stack in snapshot.stacks:
                ensure(stack_device(coordinator, stack))
    if "gpu" not in snapshot.failed:
        for gpu in snapshot.gpus:
            for metric in ("util", "vram", "temp", "power"):
                target("sensor", f"gpu_{metric}_{gpu.index}", runtime.server_device_id)
            suffix = f"gpu_{gpu.index}"
            if (device := by_identifier.get((DOMAIN, entry.entry_id + "_" + suffix))) and owned_legacy(device, suffix):
                cleanup.add(device.id)
    if "shares" not in snapshot.failed:
        for share in snapshot.shares:
            device_id = ensure(share_device(coordinator, share))
            for metric in ("used", "free"):
                target("sensor", f"share_{metric}_{share.name}", device_id)
    if "disks" not in snapshot.failed:
        for disk in snapshot.disks:
            ensure(disk_device(coordinator, disk))
    if "vms" not in snapshot.failed:
        for vm in snapshot.vms:
            ensure(vm_device(coordinator, vm))

    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if entity.platform != DOMAIN:
            continue
        target_id = targets.get((entity.domain, entity.unique_id))
        if target_id is None or entity.device_id == target_id:
            continue
        changes: dict[str, Any] = {"device_id": target_id}
        if entity.device_id and (old := devices.async_get(entity.device_id)):
            if entity.area_id is None and old.area_id is not None:
                changes["area_id"] = old.area_id
            if old.labels - entity.labels:
                changes["labels"] = entity.labels | old.labels
            if entity.disabled_by == er.RegistryEntryDisabler.DEVICE or (
                entity.disabled_by is None and old.disabled_by is not None
            ):
                changes["disabled_by"] = er.RegistryEntryDisabler.USER
        entities.async_update_entity(entity.entity_id, **changes)

    for device_id in cleanup:
        # Include disabled entries and other integrations; never remove entities.
        if not er.async_entries_for_device(entities, device_id, include_disabled_entities=True):
            devices.async_remove_device(device_id)
