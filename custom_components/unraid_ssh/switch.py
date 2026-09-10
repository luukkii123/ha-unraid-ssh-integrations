"""Switches: container start/stop, stack up/down, VM start/shutdown."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import actions
from .const import CONTAINER_ACTION_TIMEOUT, STACK_ACTION_TIMEOUT, VM_ACTION_TIMEOUT
from .coordinator import UnraidConfigEntry, UnraidCoordinator
from .entity import UnraidEntity, container_device, stack_device, track_new, vm_device
from .model import (
    Snapshot,
    find_container,
    find_stack_by_key,
    find_vm,
    stack_key,
    stack_switchable,
)


@dataclass(frozen=True, kw_only=True)
class UnraidSwitchDescription(SwitchEntityDescription):
    is_on_fn: Callable[[Any], bool]
    on_cmd: Callable[[Any], str]
    off_cmd: Callable[[Any], str]
    timeout: float


CONTAINER = UnraidSwitchDescription(
    key="container",
    is_on_fn=lambda c: c.state == "running",
    on_cmd=lambda c: actions.container_start_cmd(c.name),
    off_cmd=lambda c: actions.container_stop_cmd(c.name),
    timeout=CONTAINER_ACTION_TIMEOUT,
)
STACK = UnraidSwitchDescription(
    key="stack",
    is_on_fn=lambda s: s.running > 0,
    on_cmd=actions.stack_up_cmd,
    off_cmd=actions.stack_down_cmd,
    timeout=STACK_ACTION_TIMEOUT,
)
VM = UnraidSwitchDescription(
    key="vm",
    is_on_fn=lambda v: v.state == "running",
    on_cmd=lambda v: actions.vm_start_cmd(v.name),
    off_cmd=lambda v: actions.vm_shutdown_cmd(v.name),
    timeout=VM_ACTION_TIMEOUT,
)


class UnraidSwitch(UnraidEntity, SwitchEntity):
    entity_description: UnraidSwitchDescription

    @property
    def is_on(self) -> bool | None:
        item = self.item
        return None if item is None else self.entity_description.is_on_fn(item)

    async def _run(self, build: Callable[[Any], str]) -> None:
        item = self.item
        if item is None:
            raise HomeAssistantError("unraid_ssh: object no longer present on the server")
        try:
            await actions.run_action(self.coordinator.client, build(item), self.entity_description.timeout)
        except actions.ActionError as err:
            raise HomeAssistantError(f"unraid_ssh: {err}") from err
        finally:
            # Also after a failure: a `stack up` that ran into its timeout has
            # very likely started containers, and the state must not lag behind.
            await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._run(self.entity_description.on_cmd)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run(self.entity_description.off_cmd)


def _build(coordinator: UnraidCoordinator) -> Callable[[Snapshot], dict[str, UnraidSwitch]]:
    def build(snapshot: Snapshot) -> dict[str, UnraidSwitch]:
        out: dict[str, UnraidSwitch] = {}
        for stack in snapshot.stacks:
            device = stack_device(coordinator, stack)
            if stack_switchable(stack):
                # Unique id, device and lookup all on the stable key:
                # `stack.name` changes when the stack goes down, and an entity
                # that changes its unique id leaves the old one behind as a
                # dead entity in every automation that used it -- while one
                # that still looks itself up by the old name would go
                # unavailable exactly when someone wants to switch it on.
                key = stack_key(stack)
                out[f"stack_{key}"] = UnraidSwitch(
                    coordinator, STACK, device, f"stack_{key}", key, find_stack_by_key,
                    use_device_name=True,
                )
            for c in stack.containers:
                out[f"container_{c.name}"] = UnraidSwitch(
                    coordinator, CONTAINER, device, f"container_{c.name}", c.name, find_container, {"container": c.name}
                )
        for c in snapshot.template_containers:
            out[f"container_{c.name}"] = UnraidSwitch(
                coordinator, CONTAINER, container_device(coordinator, c), f"container_{c.name}", c.name, find_container,
                use_device_name=True,
            )
        for vm in snapshot.vms:
            out[f"vm_{vm.name}"] = UnraidSwitch(coordinator, VM, vm_device(coordinator, vm), f"vm_{vm.name}", vm.name, find_vm)
        return out

    return build


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    track_new(coordinator, async_add_entities, _build(coordinator))
