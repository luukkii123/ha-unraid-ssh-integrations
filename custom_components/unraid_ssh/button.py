"""Image checks and native container/Compose restart actions."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from functools import partial
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import actions
from .const import CONTAINER_ACTION_TIMEOUT, STACK_ACTION_TIMEOUT
from .coordinator import UnraidConfigEntry, UnraidCoordinator
from .entity import (
    ContainerPictureMixin, UnraidEntity, can_register_container,
    device_for_container, server_device, stack_device, track_new,
)
from .model import (
    Snapshot, container_key, find_container_by_key, find_stack_by_key,
    stack_key, stack_metadata, stack_restartable,
)
from .parse import Container

CHECK = ButtonEntityDescription(key="check_updates")


class CheckUpdatesButton(UnraidEntity, ButtonEntity):
    """Runs the slow update coordinator on demand."""

    async def async_press(self) -> None:
        updates = self.coordinator.entry.runtime_data.updates
        await updates.async_check_now()
        if not updates.last_update_success:
            raise HomeAssistantError(f"unraid_ssh: update check failed: {updates.last_exception}")


CONTAINER_RESTART = ButtonEntityDescription(key="container_restart")
STANDALONE_RESTART = ButtonEntityDescription(key="container_restart", translation_key="standalone_restart")
STACK_RESTART = ButtonEntityDescription(key="stack_restart")


class RestartButton(UnraidEntity, ButtonEntity):
    @property
    def available(self) -> bool:
        return super().available and (self.entity_description.key == "container_restart" or stack_restartable(self.item))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return stack_metadata(self.item, self.coordinator.entry.entry_id, "restart") if self.item else None

    async def async_press(self) -> None:
        if not self.available:
            raise HomeAssistantError("unraid_ssh: restart target is unavailable")
        container = self.entity_description.key == "container_restart"
        command = actions.container_restart_cmd(self.item.name) if container else actions.stack_restart_cmd(self.item)
        try:
            await actions.run_action(self.coordinator.client, command,
                                     CONTAINER_ACTION_TIMEOUT if container else STACK_ACTION_TIMEOUT)
        except actions.ActionError as err:
            raise HomeAssistantError(f"unraid_ssh: {err}") from err
        finally:
            await self.coordinator.async_request_refresh()


class ContainerRestartButton(ContainerPictureMixin, RestartButton):
    _role = "restart"

    def __init__(self, coordinator: UnraidCoordinator, container: Container) -> None:
        self._icons = coordinator.entry.runtime_data.icons
        self._container_name = container.name
        self._container_key = container_key(coordinator.data, container)
        description = CONTAINER_RESTART if container.project else STANDALONE_RESTART
        super().__init__(
            coordinator, description, device_for_container(coordinator, container),
            f"restart_container_{self._container_key}", self._container_key, find_container_by_key,
            {"container": container.name},
        )


def _plan(coordinator: UnraidCoordinator, snapshot: Snapshot) -> Iterator[tuple[str, Callable[[], ButtonEntity] | None]]:
    yield CHECK.key, partial(CheckUpdatesButton, coordinator, CHECK, server_device(coordinator), CHECK.key)
    for container in snapshot.containers:
        key = f"restart_container_{container_key(snapshot, container)}"
        make = partial(ContainerRestartButton, coordinator, container)
        yield key, make if can_register_container(coordinator, container, "button") else None
    readable = not {"docker", "compose", "stacks"} & snapshot.failed
    for stack in snapshot.stacks:
        key = stack_key(stack)
        make = partial(RestartButton, coordinator, STACK_RESTART, stack_device(coordinator, stack),
                       f"restart_stack_{key}", key, find_stack_by_key)
        yield f"restart_stack_{key}", make if readable and stack_restartable(stack) else None


def expected_keys(coordinator: UnraidCoordinator, snapshot: Snapshot) -> set[str]:
    return {key for key, _ in _plan(coordinator, snapshot)}


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    track_new(coordinator, async_add_entities,
              lambda snapshot: {key: make() for key, make in _plan(coordinator, snapshot) if make is not None})
