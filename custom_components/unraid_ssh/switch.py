"""Switches: container start/stop, stack up/down, VM start/shutdown."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from functools import partial
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import actions
from .const import CONTAINER_ACTION_TIMEOUT, STACK_ACTION_TIMEOUT, VM_ACTION_TIMEOUT
from .coordinator import UnraidConfigEntry, UnraidCoordinator
from .entity import ContainerPictureMixin, UnraidEntity, can_register_container, device_for_container, stack_device, track_new, vm_device
from .parse import Container
from .model import (
    Snapshot,
    find_container_by_key,
    container_key,
    stack_metadata,
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
#: A standalone container is alone on its device, so the device already says
#: which container it is and the switch needs only to say what it switches.
STANDALONE_CONTAINER = replace(CONTAINER, translation_key="standalone_container")
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
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key == "stack" and self.item is not None:
            return stack_metadata(self.item, self.coordinator.entry.entry_id, "control")
        return None

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


class ContainerSwitch(ContainerPictureMixin, UnraidSwitch):
    """Only individual containers carry a container picture."""

    def __init__(self, coordinator: UnraidCoordinator, container: Container) -> None:
        self._icons = coordinator.entry.runtime_data.icons
        self._container_name = container.name
        self._container_key = container_key(coordinator.data, container)
        # Named "Status" rather than after the device: a switch that shares its
        # name with its device reads as the device itself, and before 0.5.0 a
        # VM's switch was called "Strom" -- neither says that it starts and
        # stops something, which is what on/off means here.
        super().__init__(
            coordinator, CONTAINER if container.project else STANDALONE_CONTAINER,
            device_for_container(coordinator, container),
            f"container_{self._container_key}", self._container_key, find_container_by_key,
            {"container": container.name},
        )


Planned = tuple[str, Callable[[], UnraidSwitch] | None]


def _plan_container(coordinator: UnraidCoordinator, container: Container) -> Iterator[Planned]:
    """A container's switch, or its key alone while its device is uncertain.

    `can_register_container` withholds a brand-new switch while the stack
    metadata is missing, and that is exactly the moment the key must still
    count as expected: the container is demonstrably there, only its device is
    not yet decidable.
    """
    key = f"container_{container_key(coordinator.data, container)}"
    make = partial(ContainerSwitch, coordinator, container)
    yield key, make if can_register_container(coordinator, container, "switch") else None


def _plan(coordinator: UnraidCoordinator, snapshot: Snapshot) -> Iterator[Planned]:
    """One walk for both the platform and the orphan cleanup -- see `sensor._plan`."""
    readable = not {"docker", "compose", "stacks"} & snapshot.failed
    for stack in snapshot.stacks:
        device = stack_device(coordinator, stack)
        # Unique id, device and lookup all on the stable key: `stack.name`
        # changes when the stack goes down, and an entity that changes its
        # unique id leaves the old one behind as a dead entity in every
        # automation that used it -- while one that still looks itself up by
        # the old name would go unavailable exactly when someone wants to
        # switch it on.
        key = stack_key(stack)
        make = partial(
            UnraidSwitch, coordinator, STACK, device, f"stack_{key}", key, find_stack_by_key,
        )
        # A stack without a compose file gets no switch (`stack_switchable`),
        # but it exists, so its key stays expected: a stack that is merely down
        # is not an orphan.
        yield f"stack_{key}", make if readable and stack_switchable(stack) else None
        for container in stack.containers:
            yield from _plan_container(coordinator, container)
    for container in snapshot.template_containers:
        yield from _plan_container(coordinator, container)
    for vm in snapshot.vms:
        key = f"vm_{vm.name}"
        yield key, partial(UnraidSwitch, coordinator, VM, vm_device(coordinator, vm), key, vm.name, find_vm)


def expected_keys(coordinator: UnraidCoordinator, snapshot: Snapshot) -> set[str]:
    return {key for key, _ in _plan(coordinator, snapshot)}


def _build(coordinator: UnraidCoordinator) -> Callable[[Snapshot], dict[str, UnraidSwitch]]:
    def build(snapshot: Snapshot) -> dict[str, UnraidSwitch]:
        return {key: make() for key, make in _plan(coordinator, snapshot) if make is not None}

    return build


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    track_new(coordinator, async_add_entities, _build(coordinator))
