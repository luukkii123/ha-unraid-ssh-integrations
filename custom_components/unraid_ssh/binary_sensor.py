"""Binary sensors: array, parity, mover, disk spin state."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import UnraidConfigEntry, UnraidCoordinator
from .entity import UnraidEntity, disk_device, server_device, track_new
from .model import Snapshot, find_disk


@dataclass(frozen=True, kw_only=True)
class UnraidBinaryDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[Any], bool | None]


SERVER: tuple[UnraidBinaryDescription, ...] = (
    UnraidBinaryDescription(key="array_started", device_class=BinarySensorDeviceClass.RUNNING,
                            is_on_fn=lambda s: s.array.array_started if s.array else None),
    UnraidBinaryDescription(key="parity_running", device_class=BinarySensorDeviceClass.RUNNING,
                            is_on_fn=lambda s: s.array.parity_running if s.array else None),
    UnraidBinaryDescription(key="mover_active", device_class=BinarySensorDeviceClass.RUNNING,
                            is_on_fn=lambda s: s.array.mover_active if s.array else None),
)

DISK: tuple[UnraidBinaryDescription, ...] = (
    UnraidBinaryDescription(key="disk_spundown", is_on_fn=lambda d: d.spundown),
)


class UnraidBinarySensor(UnraidEntity, BinarySensorEntity):
    entity_description: UnraidBinaryDescription

    @property
    def is_on(self) -> bool | None:
        item = self.item
        return None if item is None else self.entity_description.is_on_fn(item)


def _plan(coordinator: UnraidCoordinator, snapshot: Snapshot) -> Iterator[tuple[str, Callable[[], Any]]]:
    """One walk for both the platform and the orphan cleanup -- see `sensor._plan`."""
    server = server_device(coordinator)
    for desc in SERVER:
        yield desc.key, partial(UnraidBinarySensor, coordinator, desc, server, desc.key)
    for disk in snapshot.disks:
        device = disk_device(coordinator, disk)
        for desc in DISK:
            key = f"{desc.key}_{disk.name}"
            yield key, partial(UnraidBinarySensor, coordinator, desc, device, key, disk.name, find_disk)


def expected_keys(coordinator: UnraidCoordinator, snapshot: Snapshot) -> set[str]:
    return {key for key, _ in _plan(coordinator, snapshot)}


def _build(coordinator: UnraidCoordinator) -> Callable[[Snapshot], dict[str, UnraidBinarySensor]]:
    def build(snapshot: Snapshot) -> dict[str, UnraidBinarySensor]:
        return {key: make() for key, make in _plan(coordinator, snapshot)}

    return build


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    track_new(coordinator, async_add_entities, _build(coordinator))
