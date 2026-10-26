"""Sensors on the server, GPU, disk, share and VM level."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, replace
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
from .entity import VmMetadataMixin, ContainerPictureMixin, can_register_container, device_for_container, UnraidEntity, disk_device, server_device, share_device, track_new, vm_device
from .model import (
    Snapshot,
    container_key, find_container_by_key,
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


class UpdateQueueSensor(UnraidEntity, SensorEntity):
    @property
    def native_value(self):
        q = self.coordinator.entry.runtime_data.queue.summary()
        return "blocked" if q["blocked"] else "active" if q["active"] else "idle"

    @property
    def extra_state_attributes(self):
        return self.coordinator.entry.runtime_data.queue.summary()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.entry.runtime_data.queue.listen(self.async_write_ha_state))


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
    UnraidSensorDescription(key="disk_total", device_class=SensorDeviceClass.DATA_SIZE,
                            native_unit_of_measurement=UnitOfInformation.KIBIBYTES,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                            value_fn=lambda d: d.fs_size_kib if d.fs_size_kib else None),
    UnraidSensorDescription(key="disk_used", device_class=SensorDeviceClass.DATA_SIZE,
                            native_unit_of_measurement=UnitOfInformation.KIBIBYTES,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                            value_fn=lambda d: d.fs_size_kib - d.fs_free_kib
                            if d.fs_size_kib and d.fs_free_kib is not None else None),
    UnraidSensorDescription(key="disk_free", device_class=SensorDeviceClass.DATA_SIZE,
                            native_unit_of_measurement=UnitOfInformation.KIBIBYTES,
                            state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=0,
                            value_fn=lambda d: d.fs_free_kib if d.fs_size_kib else None),
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



class VmStateSensor(VmMetadataMixin, UnraidSensor):
    _role = "state"


def resource_description(kind: str, role: str) -> UnraidSensorDescription:
    return UnraidSensorDescription(
        key=f"{kind}_{role}", native_unit_of_measurement=PERCENTAGE if role == "cpu" else UnitOfInformation.BYTES,
        device_class=None if role == "cpu" else SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT, suggested_display_precision=1 if role == "cpu" else 0,
        value_fn=lambda value: value,
    )


class ResourceSensor(UnraidSensor):
    @property
    def native_value(self) -> Any:
        item = self.item
        if item is None or item.state != "running":
            return None
        samples = self.snapshot.container_resources if self.entity_description.key.startswith("container_") else self.snapshot.vm_resources
        sample = samples.get(item.name)
        field = "cpu_percent" if self._role == "cpu" else self._role
        return getattr(sample, field, None)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class ContainerResourceSensor(ContainerPictureMixin, ResourceSensor):
    def __init__(self, coordinator: UnraidCoordinator, container: Any, role: str) -> None:
        self._role = role
        self._icons = coordinator.entry.runtime_data.icons
        self._container_name = container.name
        self._container_key = container_key(coordinator.data, container)
        desc = resource_description("container", role)
        if not container.project:
            desc = replace(desc, translation_key=f"standalone_{role}")
        super().__init__(coordinator, desc, device_for_container(coordinator, container),
                         f"container_{role}_{self._container_key}", self._container_key, find_container_by_key,
                         {"container": container.name})


class VmResourceSensor(VmMetadataMixin, ResourceSensor):
    def __init__(self, coordinator: UnraidCoordinator, vm: Any, role: str) -> None:
        self._role = role
        super().__init__(coordinator, resource_description("vm", role), vm_device(coordinator, vm),
                         f"vm_{role}_{vm.name}", vm.name, find_vm)

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


DASHBOARD = {
    "dashboard_cpu": (("cores", "power_packages"), ("dash_cpu", "dash_power")),
    "dashboard_memory": (("memory",), ("dash_memory",)),
    "dashboard_hardware": (("hardware", "gpu_info", "registration"), ("dash_hardware", "dash_gpu", "var")),
    "dashboard_interfaces": (("interfaces",), ("dash_interfaces",)),
    "dashboard_storage": (("storage", "pools", "docker_mode"), ("dash_storage", "dash_mounts", "dash_pools", "dash_docker")),
    "dashboard_shares": (("shares",), ("dash_shares", "dash_streams", "shares")),
    "dashboard_vms": (("vms",), ("vm_stats", "vms")),
}
_PUBLIC_KEYS = {"power_packages": "packages", "gpu_info": "gpus", "storage": "filesystems"}


class DashboardSensor(UnraidEntity, SensorEntity):
    """Current dashboard projections whose lists never enter Recorder."""
    # HA freezes exclusions in Entity.__init_subclass__; setting them on an
    # instance is too late. The explicit union covers every projection here.
    _unrecorded_attributes = frozenset({
        "cores", "packages", "memory", "hardware", "gpus", "registration",
        "interfaces", "filesystems", "pools", "docker_mode", "shares", "vms",
        "sampled_at", "failed_sections",
    })

    def __init__(self, coordinator, key):
        fields, self._sources = DASHBOARD[key]
        self._fields = fields
        super().__init__(coordinator, SensorEntityDescription(key=key), server_device(coordinator), key)

    @property
    def native_value(self):
        if self.snapshot is None: return "unavailable"
        data = self.snapshot.telemetry
        present = [getattr(data,key) is not None and (key != "docker_mode" or data.docker_mode != "unknown") for key in self._fields]
        failures = set(self._sources) & (data.failed | self.snapshot.failed)
        return "unavailable" if not any(present) else "partial" if failures or not all(present) else "ok"

    @property
    def extra_state_attributes(self):
        if self.snapshot is None: return None
        data = self.snapshot.telemetry
        times = {source:data.sample_times[source] for source in self._sources if source in data.sample_times}
        return {"schema_version":1, "sampled_at":times,
                "failed_sections":sorted(set(self._sources) & (data.failed | self.snapshot.failed)),
                **{_PUBLIC_KEYS.get(key,key):getattr(data,key) for key in self._fields}}



class InventorySensor(UnraidEntity, SensorEntity):
    """Numeric counts with current inventories excluded from Recorder."""
    _unrecorded_attributes = frozenset({'users', 'devices', 'sampled_at', 'failed_sections',
                                      'smb_enabled', 'unprotected_count', 'mapping_status'})

    def __init__(self, coordinator, kind):
        self._kind = kind
        super().__init__(coordinator, SensorEntityDescription(key='dashboard_' + kind),
                         server_device(coordinator), 'dashboard_' + kind)

    @property
    def native_value(self):
        rows = getattr(self.snapshot.inventory, self._kind) if self.snapshot else None
        return len(rows) if rows is not None else None

    @property
    def available(self):
        return super().available and self.native_value is not None

    @property
    def extra_state_attributes(self):
        if self.snapshot is None: return None
        data = self.snapshot.inventory
        sources = {'inv_' + self._kind}
        if self._kind == 'usb': sources.add('inv_usb_mapping')
        if self._kind == 'coral': sources.add('inv_usb')
        rows = getattr(data, self._kind)
        attrs = {'schema_version': 1,
                 'sampled_at': {key: value for key, value in data.sample_times.items() if key in sources},
                 'failed_sections': sorted(sources & data.failed),
                 'users' if self._kind == 'users' else 'devices': tuple(asdict(row) for row in rows) if rows is not None else None}
        if self._kind == 'users':
            attrs.update(smb_enabled=data.smb_enabled,
                         unprotected_count=sum(not row.protected for row in rows) if rows is not None else None)
        if self._kind == 'usb': attrs['mapping_status'] = data.mapping_status
        return attrs


def _coral_device(snapshot, identifier):
    return next((row for row in snapshot.inventory.coral or () if row.id == identifier), None)


def _plan_inventory(coordinator, snapshot, server):
    for kind in ('users', 'usb', 'coral'):
        yield 'dashboard_' + kind, partial(InventorySensor, coordinator, kind)
    ids = set(snapshot.inventory.known_coral_ids) | {row.id for row in snapshot.inventory.coral or () if row.transport == 'pci'}
    for identifier in sorted(ids):
        row = _coral_device(snapshot, identifier)
        for role in ('temp', 'status'):
            key = 'coral_' + role + '_' + identifier.encode('utf-8').hex()
            desc = UnraidSensorDescription(key='coral_' + role,
                device_class=SensorDeviceClass.TEMPERATURE if role == 'temp' else SensorDeviceClass.ENUM,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS if role == 'temp' else None,
                state_class=SensorStateClass.MEASUREMENT if role == 'temp' else None,
                options=None if role == 'temp' else ['ready', 'error', 'unknown'],
                value_fn=lambda row, field='temperature_c' if role == 'temp' else 'state': getattr(row, field))
            yield key, partial(DashboardNumber, coordinator, desc, server, key, identifier, _coral_device,
                               {'coral': row.name if row else identifier})

def _power(snapshot):
    packages=snapshot.telemetry.power_packages
    if not packages or any(row['power_w'] is None for row in packages): return None
    return sum(row['power_w'] for row in packages)


def _usage(snapshot,kind):
    return next((row['percent'] for row in snapshot.telemetry.storage or () if row['kind']==kind),None)


def _interface(snapshot,key):
    return next((row for row in snapshot.telemetry.interfaces or () if row['id']==key),None)


def _smart(snapshot,key):
    return next((row for row in snapshot.telemetry.smart or () if row['name']==key),None)


class DashboardNumber(UnraidSensor):
    @property
    def available(self):
        return super().available and self.native_value is not None


class SmartSensor(DashboardNumber):
    _unrecorded_attributes=frozenset({"details"})


def _plan_dashboard(coordinator,snapshot,server):
    for key in DASHBOARD:
        yield key,partial(DashboardSensor,coordinator,key)
    desc=UnraidSensorDescription(key="cpu_power",device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,state_class=SensorStateClass.MEASUREMENT,value_fn=_power)
    yield "cpu_power",partial(DashboardNumber,coordinator,desc,server,"cpu_power")
    for kind in ("boot","log","docker_vdisk"):
        key=kind+"_usage"
        desc=UnraidSensorDescription(key=key,native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,value_fn=partial(_usage,kind=kind))
        yield key,partial(DashboardNumber,coordinator,desc,server,key)
    keys=set(snapshot.telemetry.known_ids.get("dash_interfaces",())) | {row['id'] for row in snapshot.telemetry.interfaces or ()}
    for identifier in sorted(keys):
        row=_interface(snapshot,identifier)
        name=row['name'] if row else bytes.fromhex(identifier).decode('utf-8')
        for direction in ("rx","tx"):
            key=f"net_{direction}_{identifier}"
            desc=UnraidSensorDescription(key="net_"+direction,native_unit_of_measurement="B/s",
                device_class=SensorDeviceClass.DATA_RATE,state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda row,field=direction+"_bytes_s":row.get(field))
            yield key,partial(DashboardNumber,coordinator,desc,server,key,identifier,_interface,{"interface":name})
    smart_names=set(snapshot.telemetry.known_ids.get("dash_smart",())) | {row['name'] for row in snapshot.telemetry.smart or ()}
    for name in sorted(smart_names):
        key="disk_smart_"+name
        disk=find_disk(snapshot,name)
        desc=UnraidSensorDescription(key="disk_smart",device_class=SensorDeviceClass.ENUM,
            options=["passed","warning","failed","unknown"],value_fn=lambda row:row['status'],
            attrs_fn=lambda row:{k:row.get(k) for k in ("sampled_at","source","stale","details")})
        yield key,partial(SmartSensor,coordinator,desc,disk_device(coordinator,disk),key,name,_smart) if disk else None


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
    yield from _plan_dashboard(coordinator,snapshot,server)
    yield from _plan_inventory(coordinator,snapshot,server)
    yield "update_queue", partial(UpdateQueueSensor, coordinator, SensorEntityDescription(key="update_queue"), server, "update_queue")
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
            # own. Keep keys expected during FS loss without creating bogus
            # zero capacities on members, parity drives or empty filesystems.
            no_fs = not disk.fs_size_kib
            yield key, None if desc.key in {"disk_usage", "disk_total", "disk_used", "disk_free"} and no_fs else make
    for container in snapshot.containers:
        for role in ("cpu", "ram_used", "ram_limit"):
            key = f"container_{role}_{container_key(snapshot, container)}"
            make = partial(ContainerResourceSensor, coordinator, container, role)
            yield key, make if can_register_container(coordinator, container, "sensor", f"container_{role}") else None
    for vm in snapshot.vms:
        for role in ("cpu", "ram_allocated", "ram_max", "ram_host_rss", "ram_used"):
            yield f"vm_{role}_{vm.name}", partial(VmResourceSensor, coordinator, vm, role)
        for desc in VM:
            key = f"{desc.key}_{vm.name}"
            yield key, partial(VmStateSensor, coordinator, desc, vm_device(coordinator, vm), key, vm.name, find_vm)


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
