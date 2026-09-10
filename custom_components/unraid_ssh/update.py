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

    def __init__(self, updates: UpdateCoordinator, fast: UnraidCoordinator, name: str) -> None:
        super().__init__(updates)
        self.entity_description = DESCRIPTION
        self._fast = fast
        self._name = name
        self._attr_unique_id = f"{updates.entry.entry_id}_update_{name}"
        container = find_container(fast.data, name) if fast.data else None
        stack = find_stack(fast.data, container.project) if container and container.project and fast.data else None
        if container is None:
            container = Container(name, "unknown", "", "", "")   # seen by the slow poll only
        if stack is not None:
            # One stack device carries several update entities, so each one has
            # to name its container to stay distinguishable.
            self._attr_device_info = stack_device(fast, stack.name)
            self._attr_translation_key = "container_update"
            self._attr_translation_placeholders = {"container": name}
        else:
            # A device of its own, already named after the container: repeating
            # the name here would read "nginx nginx" -- and that repetition is
            # baked into the entity id at creation time.
            self._attr_device_info = container_device(fast, container)
            self._attr_translation_key = "image_update"

    @property
    def _status(self) -> ImageStatus | None:
        return self.coordinator.data.images.get(self._name) if self.coordinator.data else None

    @property
    def supported_features(self) -> UpdateEntityFeature:
        """Install where it can work, comparison everywhere else.

        Read live from the fast snapshot instead of frozen at creation time: a
        stack that gains its configuration files back (a downed compose.manager
        project started again) regains install without reloading the entry, and
        a container that only the slow poll ever saw -- the placeholder from
        `__init__` -- offers no install button that could only fail.
        """
        snapshot = self._fast.data
        container = find_container(snapshot, self._name) if snapshot else None
        if container is None:
            return UpdateEntityFeature(0)
        stack = find_stack(snapshot, container.project) if container.project else None
        return UpdateEntityFeature.INSTALL if container_updatable(container, stack) else UpdateEntityFeature(0)

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
        """Pull and recreate. Can take minutes; the timeout is UPDATE_TIMEOUT.

        Progress is not flagged here on purpose: this entity does not declare
        `UpdateEntityFeature.PROGRESS`, and for that case Home Assistant's own
        `UpdateEntity.async_install_with_progress` sets its internal flag before
        calling this and clears it in a `finally` -- and `state_attributes` then
        reports *that* flag, not `_attr_in_progress`. Setting the attribute here
        would be invisible.
        """
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
        finally:
            # Also after a failure, the way switch._run does it: a pull + up -d
            # that ran into its timeout has very likely recreated the container
            # already. Without this the counter and this entity would keep
            # claiming "update available" for up to six hours.
            await self._fast.async_request_refresh()
            await self.coordinator.async_request_refresh()


def _build(updates: UpdateCoordinator, fast: UnraidCoordinator) -> Callable[[UpdateState], dict[str, ContainerUpdate]]:
    def build(state: UpdateState) -> dict[str, ContainerUpdate]:
        return {name: ContainerUpdate(updates, fast, name) for name in state.images}

    return build


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = entry.runtime_data
    track_new(runtime.updates, async_add_entities, _build(runtime.updates, runtime.coordinator))
