"""One update entity per container whose image has a registry digest.

The entity always shows the comparison (installed digest vs. the one the
registry offers). INSTALL is offered only where an update command can actually
run -- see `model.container_updatable`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.update import (
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import actions
from .const import UPDATE_TIMEOUT
from .coordinator import UnraidConfigEntry, UnraidCoordinator, UpdateCoordinator, UpdateState
from .entity import container_device, stack_device, track_new
from .model import ImageStatus, container_updatable, find_container, find_stack
from .parse import Container

DESCRIPTION = UpdateEntityDescription(key="container_update")


def _short(digest: str | None) -> str | None:
    return None if digest is None else digest.removeprefix("sha256:")[:12]


class ContainerUpdate(CoordinatorEntity[UpdateCoordinator], UpdateEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "container_update"

    def __init__(self, updates: UpdateCoordinator, fast: UnraidCoordinator, name: str) -> None:
        super().__init__(updates)
        self.entity_description = DESCRIPTION
        self._fast = fast
        self._name = name
        self._attr_unique_id = f"{updates.entry.entry_id}_update_{name}"
        self._attr_translation_placeholders = {"container": name}
        container = find_container(fast.data, name) if fast.data else None
        stack = find_stack(fast.data, container.project) if container and container.project and fast.data else None
        if container is None:
            container = Container(name, "unknown", "", "", "")   # seen by the slow poll only
        self._attr_device_info = stack_device(fast, stack.name) if stack else container_device(fast, container)
        # Install where it can work, comparison everywhere: a compose container
        # whose stack has no configuration file cannot be pulled, and Unraid's
        # update_container script knows template containers only.
        self._attr_supported_features = (
            UpdateEntityFeature.INSTALL if container_updatable(container, stack) else UpdateEntityFeature(0)
        )

    @property
    def _status(self) -> ImageStatus | None:
        return self.coordinator.data.images.get(self._name) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        return super().available and self._status is not None

    @property
    def title(self) -> str | None:
        return self._status.image_ref if self._status else None

    @property
    def installed_version(self) -> str | None:
        return _short(self._status.local_digest) if self._status else None

    @property
    def latest_version(self) -> str | None:
        status = self._status
        if status is None:
            return None
        if status.remote_digest is None:
            return None                          # HA shows "unknown"
        return _short(status.local_digest) if status.update_available is False else _short(status.remote_digest)

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        snapshot = self._fast.data
        container = find_container(snapshot, self._name) if snapshot else None
        if container is None:
            raise HomeAssistantError(f"unraid_ssh: container {self._name} is not present")
        stack = find_stack(snapshot, container.project) if container.project else None
        if not container_updatable(container, stack):
            raise HomeAssistantError(
                f"unraid_ssh: {self._name} has no compose file to update from"
            )
        try:
            await actions.run_action(self._fast.client, actions.container_update_cmd(container, stack), UPDATE_TIMEOUT)
        except actions.ActionError as err:
            raise HomeAssistantError(f"unraid_ssh: update of {self._name} failed: {err}") from err
        await self._fast.async_request_refresh()
        await self.coordinator.async_request_refresh()


def _build(updates: UpdateCoordinator, fast: UnraidCoordinator) -> Callable[[UpdateState], dict[str, ContainerUpdate]]:
    def build(state: UpdateState) -> dict[str, ContainerUpdate]:
        return {name: ContainerUpdate(updates, fast, name) for name in state.images}

    return build


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = entry.runtime_data
    track_new(runtime.updates, async_add_entities, _build(runtime.updates, runtime.coordinator))
