"""Sensors on the server, GPU, disk, share and VM level."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import timedelta
from functools import partial
from typing import Any, Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    REVOLUTIONS_PER_MINUTE,
    UnitOfInformation,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ARRAY_STATES, DISK_STATUSES, VM_STATES
from .coordinator import UnraidConfigEntry, UnraidCoordinator, UpdateCoordinator, UpdateState
from .entity import UnraidEntity, disk_device, server_device, share_device, track_new, vm_device
from .model import (
    Snapshot,
    board_temp,
    chip_display_name,
    cpu_temp,
    find_disk,
    find_gpu,
    find_sensor,
    find_share,
    find_vm,
    is_board_chip,
)
from .parse import HwSensor, sensor_key, temp_plausible


@dataclass(frozen=True, kw_only=True)
class UnraidSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Any], Any]     # receives the item (Snapshot, Gpu, Disk or Share)
    attrs_fn: Callable[[Any], dict[str, Any]] | None = None


def _boot_time(snapshot: Snapshot) -> Any:
    if snapshot.load is None:
        return None
    boot = dt_util.utcnow() - timedelta(seconds=snapshot.load.uptime_seconds)
    return boot.replace(second=0, microsecond=0)   # steady value, no churn per poll


SERVER: tuple[UnraidSensorDescription, ...] = (
    UnraidSensorDescription(key="cpu_percent", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                            value_fn=lambda s: s.cpu_percent),
    UnraidSensorDescription(key="ram_percent", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                            value_fn=lambda s: s.memory.percent if s.memory else None),
    UnraidSensorDescription(key="load_1", state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=2,
                            value_fn=lambda s: s.load.load1 if s.load else None),
    UnraidSensorDescription(key="uptime", device_class=SensorDeviceClass.TIMESTAMP, value_fn=_boot_time),
    UnraidSensorDescription(key="array_state", device_class=SensorDeviceClass.ENUM, options=list(ARRAY_STATES),
                            value_fn=lambda s: (s.array.md_state if s.array and s.array.md_state in ARRAY_STATES else "unknown") if s.array else None),
    UnraidSensorDescription(key="parity_progress", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT,
                            value_fn=lambda s: s.array.parity_progress if s.array else None),
    UnraidSensorDescription(key="parity_errors", state_class=SensorStateClass.MEASUREMENT,
                            value_fn=lambda s: s.array.parity_errors if s.array else None),
    # Two ids that do not move. Every hwmon channel carries its chip in the id,
    # so swapping the board or going from AMD to Intel renames all of them;
    # these two keep pointing at whatever the best source happens to be, and a
    # dashboard built on them survives the swap. Created unconditionally, like
    # `cpu_percent`, and simply unknown while no source exists.
    UnraidSensorDescription(key="cpu_temp", device_class=SensorDeviceClass.TEMPERATURE,
                            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1,
                            value_fn=cpu_temp),
    UnraidSensorDescription(key="board_temp", device_class=SensorDeviceClass.TEMPERATURE,
                            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1,
                            value_fn=board_temp),
)

GPU: tuple[UnraidSensorDescription, ...] = (
    UnraidSensorDescription(key="gpu_util", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, value_fn=lambda g: g.util_percent),
    UnraidSensorDescription(key="gpu_vram", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, value_fn=lambda g: g.vram_percent),
    UnraidSensorDescription(key="gpu_temp", device_class=SensorDeviceClass.TEMPERATURE,
                            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                            state_class=SensorStateClass.MEASUREMENT, value_fn=lambda g: g.temp),
    UnraidSensorDescription(key="gpu_power", device_class=SensorDeviceClass.POWER,
                            native_unit_of_measurement=UnitOfPower.WATT,
                            state_class=SensorStateClass.MEASUREMENT, value_fn=lambda g: g.power_w),
    # nvidia-smi reports `fan.speed` as a percentage already -- no pwm maths.
    UnraidSensorDescription(key="gpu_fan", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                            value_fn=lambda g: g.fan_percent),
)

DISK: tuple[UnraidSensorDescription, ...] = (
    UnraidSensorDescription(key="disk_temp", device_class=SensorDeviceClass.TEMPERATURE,
                            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                            state_class=SensorStateClass.MEASUREMENT, value_fn=lambda d: d.temp),
    UnraidSensorDescription(key="disk_usage", native_unit_of_measurement=PERCENTAGE,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                            value_fn=lambda d: d.usage_percent),
    UnraidSensorDescription(key="disk_status", device_class=SensorDeviceClass.ENUM, options=list(DISK_STATUSES),
                            value_fn=lambda d: d.status),
)

SHARE: tuple[UnraidSensorDescription, ...] = (
    UnraidSensorDescription(key="share_used", device_class=SensorDeviceClass.DATA_SIZE,
                            native_unit_of_measurement=UnitOfInformation.KIBIBYTES,
                            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
                            suggested_display_precision=1, state_class=SensorStateClass.MEASUREMENT,
                            value_fn=lambda s: s.used_kib),
    UnraidSensorDescription(key="share_free", device_class=SensorDeviceClass.DATA_SIZE,
                            native_unit_of_measurement=UnitOfInformation.KIBIBYTES,
                            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
                            suggested_display_precision=1, state_class=SensorStateClass.MEASUREMENT,
                            value_fn=lambda s: s.free_kib),
)


def _fan_attributes(sensor: HwSensor) -> dict[str, Any]:
    """The raw drive level next to the percentage, and how the chip is driving it.

    `pwm_enable` 5 is the automatic curve the board's own fan control runs, 1
    is a fixed level somebody set. Without that distinction a reading of 60 %
    says nothing about whether the board will raise it when things get warm.
    """
    modes = {1: "manual", 5: "auto"}
    return {
        "chip": sensor.chip,
        "channel": sensor.channel,
        "pwm": sensor.pwm,
        "pwm_mode": None if sensor.pwm_enable is None else modes.get(sensor.pwm_enable, str(sensor.pwm_enable)),
    }


HW_TEMP = UnraidSensorDescription(
    key="hw_temp", device_class=SensorDeviceClass.TEMPERATURE,
    native_unit_of_measurement=UnitOfTemperature.CELSIUS,
    state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1,
    # A channel that falls out of range later reports nothing rather than a
    # number nobody can use; the entity itself stays, because the wire may well
    # come back.
    value_fn=lambda s: s.value if temp_plausible(s.value) else None,
    attrs_fn=lambda s: {"chip": s.chip, "channel": s.channel, "label": s.label},
)
FAN_RPM = UnraidSensorDescription(
    key="fan_rpm", native_unit_of_measurement=REVOLUTIONS_PER_MINUTE,
    state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
    value_fn=lambda s: s.value, attrs_fn=_fan_attributes,
)
FAN_PERCENT = UnraidSensorDescription(
    key="fan_percent", native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
    value_fn=lambda s: None if s.pwm is None else round(s.pwm / 255 * 100),
    attrs_fn=_fan_attributes,
)

#: The labels worth showing without being asked, per chip family. Everything
#: else is created but left disabled: this board alone offers twenty
#: temperature channels, and fifteen of them measure an input nobody wired.
#:
#: The channels `cpu_temp` and `board_temp` already read are deliberately
#: absent. `cpu_temp` picks the best of Tctl/Tdie/Package and `board_temp`
#: reads SYSTIN, so enabling those labels here too would put one measurement
#: into two entities under two names -- which is what the sensor list looked
#: like before 0.5.0. `CPUTIN` stays: it is the board's own reading of the
#: socket and differs from SYSTIN by fifteen degrees on real hardware.
_DEFAULT_TEMP_LABELS: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    # An NVMe reports `Composite` plus one channel per internal sensor. The
    # controller sensor runs measurably hotter than the composite average and
    # is the one that throttles, so it is worth showing; the others merely
    # repeat `Composite`.
    (("nvme",), frozenset({"Composite", "Sensor 2"})),
)

#: Labels an aggregate sensor already covers -- see `_DEFAULT_TEMP_LABELS`.
_AGGREGATED_TEMP_LABELS: Final = frozenset({"Tctl", "Tdie", "Package id 0", "SYSTIN"})


def _temp_enabled_by_default(sensor: HwSensor) -> bool:
    chip = sensor.chip.lower()
    if sensor.label in _AGGREGATED_TEMP_LABELS:
        return False
    if is_board_chip(chip):
        return sensor.label == "CPUTIN"
    return any(sensor.label in labels for prefixes, labels in _DEFAULT_TEMP_LABELS if chip.startswith(prefixes))


def _fan_display_label(sensor: HwSensor) -> str:
    """The chip's own label, else the bare channel number -- `fan4` reads as 4."""
    return sensor.label or sensor.channel.removeprefix("fan")


VM: tuple[UnraidSensorDescription, ...] = (
    UnraidSensorDescription(key="vm_state", device_class=SensorDeviceClass.ENUM, options=list(VM_STATES),
                            value_fn=lambda v: v.state),
)


class UnraidSensor(UnraidEntity, SensorEntity):
    entity_description: UnraidSensorDescription

    @property
    def native_value(self) -> Any:
        item = self.item
        return None if item is None else self.entity_description.value_fn(item)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Read live from the item, so a chip that changes its mode shows it."""
        item, attrs_fn = self.item, self.entity_description.attrs_fn
        return None if item is None or attrs_fn is None else attrs_fn(item)


class UpdatesAvailableSensor(CoordinatorEntity[UpdateCoordinator], SensorEntity):
    """How many containers have a newer image in the registry.

    It is added unconditionally, and that is load-bearing: Home Assistant
    re-arms a coordinator's interval only while it has listeners. The update
    entities come and go with the inventory, so without this one permanent
    listener the 6 h schedule would never tick again.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "updates_available"
    _attr_icon = "mdi:package-up"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, updates: UpdateCoordinator, fast: UnraidCoordinator) -> None:
        super().__init__(updates)
        self._attr_unique_id = f"{updates.entry.entry_id}_updates_available"
        self._attr_device_info = server_device(fast)

    @property
    def native_value(self) -> int | None:
        state: UpdateState | None = self.coordinator.data
        if state is None:
            return None
        return sum(1 for s in state.images.values() if s.update_available)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state: UpdateState | None = self.coordinator.data
        if state is None:
            return {}
        names = sorted(n for n, s in state.images.items() if s.update_available)
        return {
            # Capped: this list goes into the state machine and, on every
            # change, into the recorder database. The count is the state; the
            # names are a convenience, and twenty of them are enough for that.
            "containers": names[:20],
            "checked_at": state.checked_at.isoformat() if state.checked_at else None,
        }


#: A planned entity: its unique-id suffix, and how to build it. A `None`
#: factory means "this key is expected to exist, but nothing is created for it
#: right now" -- see `_plan`.
Planned = tuple[str, Callable[[], Any] | None]


def _plan(coordinator: UnraidCoordinator, snapshot: Snapshot) -> Iterator[Planned]:
    """Every unique-id suffix this platform owns, walked exactly once.

    `_build` turns this into entities and `expected_keys` into the set of ids
    the pruning in `prune.py` must keep. Both read the same list, so what is
    created and what is expected cannot drift apart -- and a drift would mean
    deleting a live entity five minutes later.

    A `None` factory is the deliberate gap between the two: the key counts as
    expected, but no entity is built. That covers a pool member without a
    filesystem, a temperature channel currently outside the plausible range and
    a GPU whose driver stopped reporting its fan. All three may come back at
    the next poll, and none of them is an orphan.
    """
    server = server_device(coordinator)
    for desc in SERVER:
        yield desc.key, partial(UnraidSensor, coordinator, desc, server, desc.key)
    for share in snapshot.shares:
        device = share_device(coordinator, share)
        for desc in SHARE:
            key = f"{desc.key}_{share.name}"
            yield key, partial(UnraidSensor, coordinator, desc, device, key, share.name, find_share)
    for gpu in snapshot.gpus:
        for desc in GPU:
            key = f"{desc.key}_{gpu.index}"
            placeholders = {"gpu": gpu.name, "index": str(gpu.index)}
            make = partial(UnraidSensor, coordinator, desc, server, key, gpu.index, find_gpu, placeholders)
            # A card without a controllable fan answers `[N/A]`; no entity, but
            # the key stays expected in case the driver starts reporting again.
            yield key, None if desc.key == "gpu_fan" and gpu.fan_percent is None else make
    for sensor in snapshot.sensors:
        yield from _plan_hw_sensor(coordinator, server, sensor)
    for disk in snapshot.disks:
        for desc in DISK:
            key = f"{desc.key}_{disk.name}"
            make = partial(UnraidSensor, coordinator, desc, disk_device(coordinator, disk), key, disk.name, find_disk)
            # Pool members (raid2, raid3, ...) carry no filesystem of their
            # own: a usage sensor there would stay unknown for good.
            yield key, None if desc.key == "disk_usage" and disk.fs_size_kib <= 0 else make
    for vm in snapshot.vms:
        for desc in VM:
            key = f"{desc.key}_{vm.name}"
            yield key, partial(UnraidSensor, coordinator, desc, vm_device(coordinator, vm), key, vm.name, find_vm)


def _plan_hw_sensor(coordinator: UnraidCoordinator, server: Any, sensor: HwSensor) -> Iterator[Planned]:
    """One temperature channel, or a fan's speed and its drive level."""
    key = sensor_key(sensor)

    def entity(desc: UnraidSensorDescription, suffix: str, placeholders: dict[str, str], enabled: bool):
        return partial(
            UnraidSensor,
            coordinator,
            desc if enabled else replace(desc, entity_registry_enabled_default=False),
            server,
            suffix,
            key,
            find_sensor,
            placeholders,
        )

    if sensor.kind == "temp":
        names = {"chip": chip_display_name(sensor.chip), "label": sensor.label or sensor.channel}
        # An input nobody wired reads -59 °C or 0 °C from the first poll on.
        # Such a channel gets no entity at all; one that merely drops out of
        # range later keeps its entity and reports nothing (see HW_TEMP).
        make = entity(HW_TEMP, f"temp_{key}", names, _temp_enabled_by_default(sensor))
        yield f"temp_{key}", make if temp_plausible(sensor.value) else None
        return
    names = {"label": _fan_display_label(sensor)}
    # Only fans that actually turn are on by default: six of the seven headers
    # on this board are empty, and each would otherwise arrive as a dead 0 RPM.
    spinning = bool(sensor.value)
    yield f"fan_{key}_rpm", entity(FAN_RPM, f"fan_{key}_rpm", names, spinning)
    if sensor.pwm is not None:
        yield f"fan_{key}_percent", entity(FAN_PERCENT, f"fan_{key}_percent", names, spinning)


def expected_keys(coordinator: UnraidCoordinator, snapshot: Snapshot) -> set[str]:
    """Unique-id suffixes this platform stands behind for the given snapshot."""
    # The counter is added unconditionally in `async_setup_entry` and is not
    # part of the walk, so it has to be named here.
    return {key for key, _ in _plan(coordinator, snapshot)} | {"updates_available"}


def _build(coordinator: UnraidCoordinator) -> Callable[[Snapshot], dict[str, UnraidSensor]]:
    def build(snapshot: Snapshot) -> dict[str, UnraidSensor]:
        return {key: make() for key, make in _plan(coordinator, snapshot) if make is not None}

    return build


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    track_new(coordinator, async_add_entities, _build(coordinator))
    async_add_entities([UpdatesAvailableSensor(entry.runtime_data.updates, coordinator)])
