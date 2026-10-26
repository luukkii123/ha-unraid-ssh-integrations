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
from .server_actions import ServerAction, ServerActionError, server_action_command
from .ssh import SSHError
from .const import CONTAINER_ACTION_TIMEOUT, STACK_ACTION_TIMEOUT, VM_ACTION_TIMEOUT
from .coordinator import UnraidConfigEntry, UnraidCoordinator
from .entity import (
    VmMetadataMixin, vm_device, ContainerPictureMixin, UnraidEntity, can_register_container,
    device_for_container, server_device, stack_device, track_new,
)
from .model import (
    Snapshot, container_updatable, find_stack, container_key, find_container_by_key, find_stack_by_key,
    find_vm, stack_key, stack_metadata, stack_restartable,
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


UPDATE_ALL = ButtonEntityDescription(key="update_all")
STACK_UPDATE_ALL = ButtonEntityDescription(key="stack_update_all")


class UpdateAllButton(UnraidEntity, ButtonEntity):
    """One atomic admission for all available, installable targets in scope."""
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        queue = self.coordinator.entry.runtime_data.queue
        if queue:
            self.async_on_remove(queue.listen(self.async_write_ha_state))

    @property
    def extra_state_attributes(self):
        runtime = self.coordinator.entry.runtime_data
        stack = self.item if self.entity_description.key == "stack_update_all" else None
        attrs = stack_metadata(stack, self.coordinator.entry.entry_id, "update_all") if stack else {"kind": "server", "role": "update_all"}
        return {**attrs, "update_queue": runtime.queue.summary(stack.name if stack else None)} if runtime.queue else attrs

    async def async_press(self):
        runtime = self.coordinator.entry.runtime_data
        snapshot = self.coordinator.data
        stack = self.item if self.entity_description.key == "stack_update_all" else None
        if not snapshot or (self.entity_description.key == "stack_update_all" and not stack):
            raise HomeAssistantError("Updateziele fehlen. Unraid-Verbindung prüfen.")
        images = runtime.updates.data.images if runtime.updates.data else {}
        containers = [c for c in snapshot.containers
                      if (not stack or c.project == stack.name)
                      and images.get(c.name) and images[c.name].update_available
                      and can_register_container(self.coordinator, c, "update")
                      and container_updatable(c, find_stack(snapshot, c.project) if c.project else None)]
        try:
            await runtime.queue.enqueue(containers)
        except actions.ActionError as err:
            raise HomeAssistantError(str(err)) from err


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



VM_RESTART = ButtonEntityDescription(key="vm_restart")
VM_FORCE_STOP = ButtonEntityDescription(key="vm_force_stop")


class VmRestartButton(VmMetadataMixin, UnraidEntity, ButtonEntity):
    _role = "restart"

    @property
    def available(self) -> bool:
        return super().available and self.item.state == "running"

    async def async_press(self) -> None:
        if not self.available:
            raise HomeAssistantError("unraid_ssh: restart target is unavailable")
        try:
            await actions.run_action(self.coordinator.client, actions.vm_restart_cmd(self.item.name), VM_ACTION_TIMEOUT)
        except actions.ActionError as err:
            raise HomeAssistantError(f"unraid_ssh: {err}") from err
        finally:
            await self.coordinator.async_request_refresh()


class VmForceStopButton(VmMetadataMixin, UnraidEntity, ButtonEntity):
    _role = "force_stop"

    @property
    def available(self) -> bool:
        return super().available and self.item.state not in {"shut_off", "unknown"}

    async def async_press(self) -> None:
        if not self.available:
            raise HomeAssistantError("unraid_ssh: force-stop target is unavailable")
        try:
            await actions.run_action(self.coordinator.client,
                                     actions.vm_force_stop_cmd(self.item.name), VM_ACTION_TIMEOUT)
        except actions.ActionError as err:
            raise HomeAssistantError(f"unraid_ssh: {err}") from err
        finally:
            await self.coordinator.async_request_refresh()

class ServerActionButton(UnraidEntity, ButtonEntity):
    """Reject concurrent requests; validate a fresh snapshot under the lock."""
    def __init__(self, coordinator, action):
        self._action=action
        super().__init__(coordinator, ButtonEntityDescription(key=action.value),
                         server_device(coordinator),action.value)

    @staticmethod
    def _error(key):
        return HomeAssistantError(translation_domain="unraid_ssh", translation_key=key)

    async def async_press(self):
        runtime=self.coordinator.entry.runtime_data
        lock=runtime.server_action_lock
        if lock.locked(): raise self._error("server_action_busy")
        # acquire() does not suspend for an unlocked asyncio.Lock. Admission is
        # reserved before the fresh poll awaits, so queue admission sees it.
        async with lock:
            if runtime.queue and runtime.queue.summary()["active"]:
                raise self._error("server_update_active")
            if self._action is ServerAction.ARRAY_START:
                await self.coordinator.async_refresh_inventory()
            else:
                await self.coordinator.async_refresh()
            if not self.coordinator.last_update_success or self.snapshot is None:
                raise self._error("server_poll_failed")
            try:
                command=server_action_command(self._action,self.snapshot)
            except ServerActionError as err:
                raise self._error(str(err)) from err
            try:
                result=await runtime.client.run(command,timeout=30)
            except (SSHError,TimeoutError) as err:
                # Sending a reboot then losing SSH is uncertain, not success.
                # In particular there is no automatic command retry.
                raise self._error("server_action_uncertain") from err
            if result.exit_status!=0:
                raise self._error("server_action_failed")


def _plan(coordinator: UnraidCoordinator, snapshot: Snapshot) -> Iterator[tuple[str, Callable[[], ButtonEntity] | None]]:
    for action in ServerAction:
        yield action.value, partial(ServerActionButton,coordinator,action)
    for vm in snapshot.vms:
        key = f"restart_vm_{vm.name}"
        yield key, partial(VmRestartButton, coordinator, VM_RESTART, vm_device(coordinator, vm), key, vm.name, find_vm)
        force_key = f"force_stop_vm_{vm.name}"
        yield force_key, partial(VmForceStopButton, coordinator, VM_FORCE_STOP,
                                 vm_device(coordinator, vm), force_key, vm.name, find_vm)
    yield UPDATE_ALL.key, partial(UpdateAllButton, coordinator, UPDATE_ALL, server_device(coordinator), UPDATE_ALL.key)
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
        yield f"update_all_stack_{key}", (partial(UpdateAllButton, coordinator, STACK_UPDATE_ALL,
            stack_device(coordinator, stack), f"update_all_stack_{key}", key, find_stack_by_key)
            if readable else None)


def expected_keys(coordinator: UnraidCoordinator, snapshot: Snapshot) -> set[str]:
    return {key for key, _ in _plan(coordinator, snapshot)}


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    track_new(coordinator, async_add_entities,
              lambda snapshot: {key: make() for key, make in _plan(coordinator, snapshot) if make is not None})
