"""Sensors on the server, GPU, disk, share and VM level."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfInformation, UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import ARRAY_STATES, DISK_STATUSES, VM_STATES
from .coordinator import UnraidConfigEntry, UnraidCoordinator
from .entity import UnraidEntity, disk_device, gpu_device, server_device, track_new, vm_device
from .model import Snapshot, find_disk, find_gpu, find_share, find_vm


@dataclass(frozen=True, kw_only=True)
class UnraidSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Any], Any]     # receives the item (Snapshot, Gpu, Disk or Share)


def _boot_time(snapshot: Snapshot) -> Any:
    if snapshot.load is None:
        return None
    boot = dt_util.utcnow() - timedelta(seconds=snapshot.load.uptime_seconds)
    return boot.replace(second=0, microsecond=0)   # steady value, no churn per poll


SERVER: tuple[UnraidSensorDescription, ...] = (
    UnraidSensorDescription(key="cpu_percent", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                            value_fn=lambda s: s.cpu_percent),
    UnraidSensorDescription(key="ram_percent", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                            value_fn=lambda s: s.memory.percent if s.memory else None),
    UnraidSensorDescription(key="load_1", state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=2,
                            value_fn=lambda s: s.load.load1 if s.load else None),
    UnraidSensorDescription(key="uptime", device_class=SensorDeviceClass.TIMESTAMP, value_fn=_boot_time),
    UnraidSensorDescription(key="array_state", device_class=SensorDeviceClass.ENUM, options=list(ARRAY_STATES),
                            value_fn=lambda s: (s.array.md_state if s.array and s.array.md_state in ARRAY_STATES else "unknown") if s.array else None),
    UnraidSensorDescription(key="parity_progress", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT,
                            value_fn=lambda s: s.array.parity_progress if s.array else None),
    UnraidSensorDescription(key="parity_errors", state_class=SensorStateClass.MEASUREMENT,
                            value_fn=lambda s: s.array.parity_errors if s.array else None),
)

GPU: tuple[UnraidSensorDescription, ...] = (
    UnraidSensorDescription(key="gpu_util", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, value_fn=lambda g: g.util_percent),
    UnraidSensorDescription(key="gpu_vram", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, value_fn=lambda g: g.vram_percent),
    UnraidSensorDescription(key="gpu_temp", device_class=SensorDeviceClass.TEMPERATURE,
                            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                            state_class=SensorStateClass.MEASUREMENT, value_fn=lambda g: g.temp),
    UnraidSensorDescription(key="gpu_power", device_class=SensorDeviceClass.POWER,
                            native_unit_of_measurement=UnitOfPower.WATT,
                            state_class=SensorStateClass.MEASUREMENT, value_fn=lambda g: g.power_w),
)

DISK: tuple[UnraidSensorDescription, ...] = (
    UnraidSensorDescription(key="disk_temp", device_class=SensorDeviceClass.TEMPERATURE,
                            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                            state_class=SensorStateClass.MEASUREMENT, value_fn=lambda d: d.temp),
    UnraidSensorDescription(key="disk_usage", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                            value_fn=lambda d: d.usage_percent),
    UnraidSensorDescription(key="disk_status", device_class=SensorDeviceClass.ENUM, options=list(DISK_STATUSES),
                            value_fn=lambda d: d.status),
)

SHARE: tuple[UnraidSensorDescription, ...] = (
    UnraidSensorDescription(key="share_used", device_class=SensorDeviceClass.DATA_SIZE,
                            native_unit_of_measurement=UnitOfInformation.KIBIBYTES,
                            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
                            suggested_display_precision=1, state_class=SensorStateClass.MEASUREMENT,
                            value_fn=lambda s: s.used_kib),
    UnraidSensorDescription(key="share_free", device_class=SensorDeviceClass.DATA_SIZE,
                            native_unit_of_measurement=UnitOfInformation.KIBIBYTES,
                            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
                            suggested_display_precision=1, state_class=SensorStateClass.MEASUREMENT,
                            value_fn=lambda s: s.free_kib),
)


VM: tuple[UnraidSensorDescription, ...] = (
    UnraidSensorDescription(key="vm_state", device_class=SensorDeviceClass.ENUM, options=list(VM_STATES),
                            value_fn=lambda v: v.state),
)


class UnraidSensor(UnraidEntity, SensorEntity):
    entity_description: UnraidSensorDescription

    @property
    def native_value(self) -> Any:
        item = self.item
        return None if item is None else self.entity_description.value_fn(item)


def _build(coordinator: UnraidCoordinator) -> Callable[[Snapshot], dict[str, UnraidSensor]]:
    def build(snapshot: Snapshot) -> dict[str, UnraidSensor]:
        out: dict[str, UnraidSensor] = {}
        server = server_device(coordinator)
        for desc in SERVER:
            out[desc.key] = UnraidSensor(coordinator, desc, server, desc.key)
        for share in snapshot.shares:
            for desc in SHARE:
                key = f"{desc.key}_{share.name}"
                out[key] = UnraidSensor(coordinator, desc, server, key, share.name, find_share, {"share": share.name})
        for gpu in snapshot.gpus:
            for desc in GPU:
                key = f"{desc.key}_{gpu.index}"
                out[key] = UnraidSensor(coordinator, desc, gpu_device(coordinator, gpu), key, gpu.index, find_gpu)
        for disk in snapshot.disks:
            for desc in DISK:
                # Pool members (raid2, raid3, ...) carry no filesystem of their
                # own: a usage sensor there would stay unknown for good.
                if desc.key == "disk_usage" and disk.fs_size_kib <= 0:
                    continue
                key = f"{desc.key}_{disk.name}"
                out[key] = UnraidSensor(coordinator, desc, disk_device(coordinator, disk), key, disk.name, find_disk)
        for vm in snapshot.vms:
            for desc in VM:
                key = f"{desc.key}_{vm.name}"
                out[key] = UnraidSensor(coordinator, desc, vm_device(coordinator, vm), key, vm.name, find_vm)
        return out

    return build


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    track_new(coordinator, async_add_entities, _build(coordinator))
