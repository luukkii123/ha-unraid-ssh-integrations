"""Entity base, device cards and the helper that adds entities as they appear."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN, ENTITY_ICONS
from .coordinator import UnraidCoordinator
from .devices import container_assignment_complete, container_device_suffix, share_device_suffix
from .icon_cache import ContainerIconCache
from .model import Snapshot, Stack, find_container, find_stack, stack_key
from .parse import Container, Disk, Share, Vm

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


def _child(
    coordinator: UnraidCoordinator,
    suffix: str,
    translation_key: str,
    model: str,
    component: str | None = None,
    **extra: Any,
) -> DeviceInfo:
    """A translated device below the server one.

    Every child carries the entry title, without exception. With
    `has_entity_name` the device name becomes the stem of every entity id
    created on it, so this is what makes the README's promise true -- the entry
    title is the prefix of all of them -- and what keeps `Windows 11` or
    `nginx` from colliding with the entity ids of the other Unraid integration,
    where Home Assistant would silently append `_2`.
    """
    placeholders = {"prefix": coordinator.entry.title}
    if component is not None:
        placeholders["component"] = component
    return DeviceInfo(
        identifiers={(DOMAIN, f"{coordinator.entry.entry_id}_{suffix}")},
        translation_key=translation_key,
        translation_placeholders=placeholders,
        manufacturer=MANUFACTURER,
        model=model,
        **_parent_device(coordinator),
        **extra,
    )


def _parent_device(coordinator: UnraidCoordinator) -> dict[str, Any]:
    """Use the parent API available in the installed HA version."""
    if "via_device_id" not in DeviceInfo.__annotations__:
        return {"via_device": _server_id(coordinator)}
    device_id = getattr(coordinator.entry.runtime_data, "server_device_id", None)
    return {"via_device_id": device_id} if device_id else {}


def _preserve_failed_device(entity: Entity, coordinator: UnraidCoordinator, info: DeviceInfo) -> DeviceInfo:
    """A partial snapshot must not let platform setup bypass migration guards."""
    snapshot = coordinator.data
    suffix = entity.unique_id.removeprefix(coordinator.entry.entry_id + "_")
    section = next((section for prefixes, section in (
        (("container_", "update_"), "docker"),
        (("gpu_util_", "gpu_vram_", "gpu_temp_", "gpu_power_", "gpu_fan_"), "gpu"),
        (("share_used_", "share_free_"), "shares"),
    ) if suffix.startswith(prefixes)), None)
    if snapshot is None or entity.hass is None:
        return info
    if section == "docker":
        name = suffix.partition("_")[2]
        failed = not container_assignment_complete(snapshot, find_container(snapshot, name))
    else:
        failed = section in snapshot.failed
    if not failed:
        return info
    registry = er.async_get(entity.hass)
    for item in er.async_entries_for_config_entry(registry, coordinator.entry.entry_id):
        if item.platform == DOMAIN and item.unique_id == entity.unique_id and item.device_id:
            if device := dr.async_get(entity.hass).async_get(item.device_id):
                return DeviceInfo(identifiers=device.identifiers)
    return info


def can_register_container(coordinator: UnraidCoordinator, container: Container, domain: str) -> bool:
    """Defer uncertain new assignments; existing entities retain their device."""
    if container_assignment_complete(coordinator.data, container):
        return True
    registry = er.async_get(coordinator.hass)
    prefix = "container" if domain == "switch" else "update"
    unique_id = f"{coordinator.entry.entry_id}_{prefix}_{container.name}"
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    if entity_id is None:
        return False
    item = registry.async_get(entity_id)
    return (
        item.config_entry_id == coordinator.entry.entry_id
        and item.device_id is not None
        and dr.async_get(coordinator.hass).async_get(item.device_id) is not None
    )


def disk_device(coordinator: UnraidCoordinator, disk: Disk) -> DeviceInfo:
    return _child(coordinator, f"disk_{disk.name}", "disk", f"{disk.kind} disk", disk.name)


def stack_device(coordinator: UnraidCoordinator, stack: Stack) -> DeviceInfo:
    """Identified by the folder basename, not by the live project name.

    `Stack.name` is the running project's name while the stack is up and a
    derived one while it is down, so keying the device on it would hand the
    same stack two devices over its lifetime -- and the older one would stay
    behind, forever unavailable. `model.stack_key` is the part that does not
    move.
    """
    key = stack_key(stack)
    return _child(coordinator, f"stack_{key}", "stack", "Compose stack", key)


def loose_containers_device(coordinator: UnraidCoordinator) -> DeviceInfo:
    return _child(coordinator, "containers", "standalone_containers", "Docker containers")


def device_for_container(coordinator: UnraidCoordinator, container: Container) -> DeviceInfo:
    """Return the shared standalone device or the container's stack device."""
    snapshot = coordinator.data
    if snapshot is None:
        raise ValueError("container device assignment requires a fast snapshot")
    suffix = container_device_suffix(snapshot, container)
    if not container.project:
        return loose_containers_device(coordinator)
    if stack := find_stack(snapshot, container.project):
        return stack_device(coordinator, stack)
    return _child(coordinator, suffix, "stack", "Compose stack", container.project)


def share_device(coordinator: UnraidCoordinator, share: Share) -> DeviceInfo:
    return _child(coordinator, share_device_suffix(share.name), "share", "Unraid share", share.name)


def vm_device(coordinator: UnraidCoordinator, vm: Vm) -> DeviceInfo:
    return _child(coordinator, f"vm_{vm.name}", "vm", "Virtual machine", vm.name)


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
    def device_info(self) -> DeviceInfo:
        return _preserve_failed_device(self, self.coordinator, self._attr_device_info)

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
    coordinator: DataUpdateCoordinator[Any],
    async_add_entities: AddEntitiesCallback,
    build: Callable[[Any], dict[str, Entity]],
    *,
    listen_to: tuple[DataUpdateCoordinator[Any], ...] = (),
) -> None:
    """Add unseen entities when primary data or a dependent coordinator changes.

    ``build`` always receives ``coordinator.data``. Platforms whose build also
    depends on another coordinator pass it through ``listen_to`` so discovery
    is retried when that secondary data becomes complete.
    """
    known: set[str] = set()

    def _sync() -> None:
        if coordinator.data is None:
            return
        fresh = {key: entity for key, entity in build(coordinator.data).items() if key not in known}
        if fresh:
            known.update(fresh)
            async_add_entities(list(fresh.values()))

    _sync()
    for source in (coordinator, *listen_to):
        coordinator.entry.async_on_unload(source.async_add_listener(_sync))


class ContainerPictureMixin:
    """Same live picture and listener lifecycle for every container entity."""

    _icons: ContainerIconCache
    _container_name: str

    @property
    def device_info(self) -> DeviceInfo:
        fast = getattr(self, "_fast", self.coordinator)
        snapshot = fast.data
        if snapshot is not None:
            if container := find_container(snapshot, self._container_name):
                if container_assignment_complete(snapshot, container):
                    self._attr_device_info = device_for_container(fast, container)
        return _preserve_failed_device(self, fast, self._attr_device_info)

    @property
    def entity_picture(self) -> str | None:
        return self._icons.picture(self._container_name)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._icons.async_add_listener(self.async_write_ha_state))
