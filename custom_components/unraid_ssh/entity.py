"""Entity base, device cards and the helper that adds entities as they appear."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ENTITY_ICONS
from .coordinator import UnraidCoordinator
from .model import Snapshot
from .parse import Container, Disk, Gpu, Vm

MANUFACTURER = "Lime Technology"


def _server_id(coordinator: UnraidCoordinator) -> tuple[str, str]:
    return (DOMAIN, coordinator.entry.entry_id)


def server_device(coordinator: UnraidCoordinator) -> DeviceInfo:
    array = coordinator.data.array if coordinator.data else None
    return DeviceInfo(
        identifiers={_server_id(coordinator)},
        name=coordinator.entry.title,
        manufacturer=MANUFACTURER,
        model="Unraid",
        sw_version=array.version if array else None,
        configuration_url=f"http://{coordinator.host}",
    )


def _child(coordinator: UnraidCoordinator, suffix: str, name: str, model: str, **extra: Any) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_{suffix}")},
        name=name,
        manufacturer=MANUFACTURER,
        model=model,
        via_device=_server_id(coordinator),
        **extra,
    )


def gpu_device(coordinator: UnraidCoordinator, gpu: Gpu) -> DeviceInfo:
    return _child(coordinator, f"gpu_{gpu.index}", gpu.name, "GPU")


def disk_device(coordinator: UnraidCoordinator, disk: Disk) -> DeviceInfo:
    return _child(coordinator, f"disk_{disk.name}", f"{coordinator.entry.title} {disk.name}", f"{disk.kind} disk")


def stack_device(coordinator: UnraidCoordinator, name: str) -> DeviceInfo:
    return _child(coordinator, f"stack_{name}", f"Stack {name}", "Compose stack")


def container_device(coordinator: UnraidCoordinator, container: Container) -> DeviceInfo:
    return _child(coordinator, f"container_{container.name}", container.name, "Docker container")


def vm_device(coordinator: UnraidCoordinator, vm: Vm) -> DeviceInfo:
    return _child(coordinator, f"vm_{vm.name}", vm.name, "Virtual machine")


class UnraidEntity(CoordinatorEntity[UnraidCoordinator]):
    """Base for every entity: device, unique id, icon, item lookup."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: UnraidCoordinator,
        description: EntityDescription,
        device: DeviceInfo,
        unique_suffix: str,
        item_key: Any = None,
        finder: Callable[[Snapshot, Any], Any] | None = None,
        placeholders: dict[str, str] | None = None,
        *,
        use_device_name: bool = False,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{unique_suffix}"
        self._attr_device_info = device
        if use_device_name:
            # The main entity of a device: name None (with has_entity_name) makes
            # HA show the device name alone. A translated name here would repeat
            # it -- "nginx nginx", "Stack buschfunk Stack" -- and that repetition
            # is baked into the entity id at creation time.
            self._attr_name = None
        else:
            self._attr_translation_key = description.translation_key or description.key
            if placeholders:
                self._attr_translation_placeholders = placeholders
        # HA supplies a default icon per device class, but not for ENUM.
        if getattr(description, "device_class", None) in (None, SensorDeviceClass.ENUM):
            self._attr_icon = ENTITY_ICONS.get(description.key)
        self._item_key = item_key
        self._finder = finder

    @property
    def snapshot(self) -> Snapshot | None:
        return self.coordinator.data

    @property
    def item(self) -> Any:
        """The current object this entity describes, or None (server-level: the snapshot)."""
        if self.snapshot is None:
            return None
        if self._finder is None:
            return self.snapshot
        return self._finder(self.snapshot, self._item_key)

    @property
    def available(self) -> bool:
        return super().available and self.item is not None


def track_new(
    coordinator: UnraidCoordinator,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[Snapshot], dict[str, Entity]],
) -> None:
    """Add entities for keys not seen before — now and on every coordinator update."""
    known: set[str] = set()

    def _sync() -> None:
        if coordinator.data is None:
            return
        fresh = {key: entity for key, entity in build(coordinator.data).items() if key not in known}
        if fresh:
            known.update(fresh)
            async_add_entities(list(fresh.values()))

    _sync()
    coordinator.entry.async_on_unload(coordinator.async_add_listener(_sync))
