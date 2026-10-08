"""One update entity per container whose image has a registry digest.

The entity always shows the comparison (installed digest vs. the one the
registry offers). INSTALL is offered only where an update command can actually
run -- see `model.container_updatable`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
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
from .coordinator import UnraidConfigEntry, UnraidCoordinator, UpdateCoordinator, UpdateState
from .entity import ContainerPictureMixin, can_register_container, device_for_container, track_new
from .model import ImageStatus, Snapshot, container_updatable, container_key, find_stack
from .parse import Container

DESCRIPTION = UpdateEntityDescription(key="container_update")


def _short(digest: str | None) -> str | None:
    return None if digest is None else digest.removeprefix("sha256:")[:12]


class ContainerUpdate(ContainerPictureMixin, CoordinatorEntity[UpdateCoordinator], UpdateEntity):
    _role = "update"
    _attr_has_entity_name = True

    def __init__(self, updates: UpdateCoordinator, fast: UnraidCoordinator, container: Container) -> None:
        super().__init__(updates)
        self.entity_description = DESCRIPTION
        self._fast = fast
        self._name = container.name
        self._container_name = container.name
        self._container_key = container_key(fast.data, container)
        self._icons = fast.entry.runtime_data.icons
        self._attr_unique_id = f"{updates.entry.entry_id}_update_{self._container_key}"
        self._attr_device_info = device_for_container(fast, container)
        self._attr_translation_key = "container_update" if container.project else "standalone_update"
        self._attr_translation_placeholders = {"container": container.name}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._fast.async_add_listener(self.async_write_ha_state))
        if self._queue:
            self.async_on_remove(self._queue.listen(self.async_write_ha_state))

    @property
    def _status(self) -> ImageStatus | None:
        container = self.current_container
        return self.coordinator.data.images.get(container.name) if self.coordinator.data and container else None

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
        container = self.current_container
        if container is None:
            return UpdateEntityFeature(0)
        stack = find_stack(snapshot, container.project) if container.project else None
        return UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS if container_updatable(container, stack) else UpdateEntityFeature.PROGRESS

    @property
    def available(self) -> bool:
        return self.in_progress or (super().available and self._status is not None)

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

    @property
    def _queue(self):
        return self._fast.entry.runtime_data.queue

    @property
    def in_progress(self) -> bool:
        from .update_queue import ACTIVE
        job = self._queue.target(self._container_key) if self._queue else None
        return bool(job and job["state"] in ACTIVE)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = super().extra_state_attributes or {}
        return {**attrs, **(self._queue.attributes(self._container_key) if self._queue else {})}

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        container = self.current_container
        if container is None:
            raise HomeAssistantError("Container fehlt. Unraid-Verbindung prüfen.")
        if not self._queue:
            raise HomeAssistantError("Update-Warteschlange fehlt. Integration neu laden.")
        try:
            await self._queue.enqueue([container])
        except actions.ActionError as err:
            raise HomeAssistantError(str(err)) from err


def _plan(
    updates: UpdateCoordinator | None,
    fast: UnraidCoordinator,
    snapshot: Snapshot,
    state: UpdateState | None,
) -> Iterator[tuple[str, Callable[[], ContainerUpdate] | None]]:
    """One walk for both the platform and the orphan cleanup.

    Every container of the *fast* snapshot yields its key, whether or not the
    slow coordinator has a digest for it yet. That first check takes minutes,
    and treating a container as orphaned in the meantime would delete the very
    entity the check is about to fill.
    """
    for container in snapshot.containers:
        make = None
        if state is not None and container.name in state.images and can_register_container(fast, container, "update"):
            make = partial(ContainerUpdate, updates, fast, container)
        yield f"update_{container_key(snapshot, container)}", make


def expected_keys(fast: UnraidCoordinator, snapshot: Snapshot) -> set[str]:
    return {key for key, _ in _plan(None, fast, snapshot, None)}


def _build(updates: UpdateCoordinator, fast: UnraidCoordinator) -> Callable[[UpdateState], dict[str, ContainerUpdate]]:
    def build(state: UpdateState) -> dict[str, ContainerUpdate]:
        snapshot = fast.data
        if snapshot is None:
            return {}
        return {key: make() for key, make in _plan(updates, fast, snapshot, state) if make is not None}

    return build


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = entry.runtime_data
    track_new(
        runtime.updates,
        async_add_entities,
        _build(runtime.updates, runtime.coordinator),
        listen_to=(runtime.coordinator,),
    )
