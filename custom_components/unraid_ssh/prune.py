"""Prune non-container entities after the successful-poll grace period.

Container controls, updates and restarts are deliberately preserved during
identity migration. Removing their consumer references requires an explicit
registry/consumer audit, not a missing Docker name. Other entity types keep
their existing section-success and grace-period protections. The clocks are
runtime-only; reloading an entry resets them without removing anything.
"""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from . import binary_sensor, button, sensor, switch, update
from .const import DOMAIN, STALE_GRACE
from .coordinator import UnraidConfigEntry
from .devices import container_device_suffix, share_device_suffix
from .model import Snapshot, stack_key

_LOGGER = logging.getLogger(__name__)

#: Which sections have to have parsed before a unique id of this shape may be
#: judged at all. A failed section means the server told us nothing about that
#: kind of thing this poll -- not that the thing is gone.
_ENTITY_SECTIONS: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    (("container_", "update_"), frozenset({"docker"})),
    # A stack can come from a compose.manager folder, from `docker compose ls`
    # or from a container's project label alone, so all three have to be
    # readable before a missing stack means anything. The spec asks only for
    # `compose` and `stacks`; `docker` is added because a stack that exists
    # purely through its containers' labels would otherwise vanish from the
    # snapshot whenever `docker ps` fails.
    (("stack_",), frozenset({"docker", "compose", "stacks"})),
    (("vm_", "vm_state_"), frozenset({"vms"})),
    (("disk_temp_", "disk_usage_", "disk_status_", "disk_spundown_"), frozenset({"disks"})),
    (("share_used_", "share_free_"), frozenset({"shares"})),
    (("gpu_util_", "gpu_vram_", "gpu_temp_", "gpu_power_", "gpu_fan_"), frozenset({"gpu"})),
    (("temp_", "fan_"), frozenset({"sensors"})),
)

#: The same question for a device identifier suffix.
_DEVICE_SECTIONS: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    (("disk_",), frozenset({"disks"})),
    (("share_",), frozenset({"shares"})),
    (("stack_",), frozenset({"docker", "compose", "stacks"})),
    (("vm_",), frozenset({"vms"})),
    # Both historic shared devices and individual standalone devices can be
    # removed manually only when the current successful snapshot excludes them.
    (("containers", "container_"), frozenset({"docker"})),
)


def _sections(table: tuple[tuple[tuple[str, ...], frozenset[str]], ...], suffix: str) -> frozenset[str] | None:
    """`None` means: a shape this cleanup does not know, so never a candidate.

    Every server-level entity lands here -- `cpu_percent`, `array_state`,
    `cpu_temp`, `check_updates` -- and so would anything a future version adds
    without a line in the table above. An unknown shape is far more likely to
    be an entity of ours that the table forgot than an orphan worth deleting,
    and the cost of being wrong is asymmetric.
    """
    return next((sections for prefixes, sections in table if suffix.startswith(prefixes)), None)


def expected_keys(entry: UnraidConfigEntry, snapshot: Snapshot) -> set[str]:
    """Every unique-id suffix the platforms stand behind for this snapshot.

    Each platform answers from the same walk it builds its entities from
    (`_plan`), so expectation and creation cannot drift apart. Where they
    differ on purpose -- a container whose device is not yet decidable, a
    stack without a compose file, a temperature channel out of range -- the
    walk still yields the key, and the entity survives.
    """
    fast = entry.runtime_data.coordinator
    return (
        sensor.expected_keys(fast, snapshot)
        | binary_sensor.expected_keys(fast, snapshot)
        | switch.expected_keys(fast, snapshot)
        | update.expected_keys(fast, snapshot)
        | button.expected_keys(fast, snapshot)
    )


def expected_device_suffixes(snapshot: Snapshot) -> set[str]:
    """The device identifier suffixes this snapshot accounts for.

    `""` is the server device; it is listed so that the membership test below
    reads the same for it as for every child, and it is additionally refused
    outright in `can_remove_device`.
    """
    suffixes = {""}
    suffixes |= {f"disk_{disk.name}" for disk in snapshot.disks}
    suffixes |= {share_device_suffix(share.name) for share in snapshot.shares}
    suffixes |= {f"stack_{stack_key(stack)}" for stack in snapshot.stacks}
    suffixes |= {f"vm_{vm.name}" for vm in snapshot.vms}
    # Standalone containers have individual devices; Compose members share
    # their stack device.
    suffixes |= {container_device_suffix(snapshot, c) for c in snapshot.containers}
    return suffixes


def _identifier(entry: UnraidConfigEntry, suffix: str) -> tuple[str, str]:
    return (DOMAIN, f"{entry.entry_id}_{suffix}" if suffix else entry.entry_id)


@callback
def async_prune_stale(hass: HomeAssistant, entry: UnraidConfigEntry) -> None:
    """Drop entities absent from every successful poll for `STALE_GRACE`."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    snapshot = coordinator.data
    if not coordinator.last_update_success or snapshot is None:
        return
    prefix = entry.entry_id + "_"
    expected = {prefix + key for key in expected_keys(entry, snapshot)}
    registry = er.async_get(hass)
    now = dt_util.utcnow()
    clocks: dict[str, datetime] = {}
    removed: list[tuple[str, str | None]] = []

    for item in er.async_entries_for_config_entry(registry, entry.entry_id):
        if item.platform != DOMAIN:
            continue
        unique_id = item.unique_id
        # Container identities are undergoing a conservative transition. Keep
        # old consumer references until an explicit registry/consumer audit.
        if unique_id.removeprefix(prefix).startswith(("container_", "update_", "restart_container_")):
            continue
        if unique_id in expected:
            continue                       # back, or never gone: the clock is dropped
        sections = _sections(_ENTITY_SECTIONS, unique_id.removeprefix(prefix))
        if sections is None:
            continue
        if sections & snapshot.failed:
            # Not judged this poll. The clock is neither started nor reset --
            # a server that cannot answer must not buy an entity more time,
            # and must not cost it any either.
            if unique_id in runtime.stale_since:
                clocks[unique_id] = runtime.stale_since[unique_id]
            continue
        since = runtime.stale_since.get(unique_id, now)
        if now - since < STALE_GRACE:
            clocks[unique_id] = since
            continue
        registry.async_remove(item.entity_id)
        removed.append((item.entity_id, item.device_id))
    runtime.stale_since = clocks

    if not removed:
        return
    minutes = int(STALE_GRACE.total_seconds() // 60)
    for entity_id, _ in removed:
        _LOGGER.info("unraid_ssh: removed %s (absent for %d min)", entity_id, minutes)
    devices = dr.async_get(hass)
    for device_id in {device_id for _, device_id in removed if device_id}:
        if device_id == runtime.server_device_id:
            continue
        device = devices.async_get(device_id)
        if device is None or device.config_entries != {entry.entry_id}:
            continue
        # Include disabled entries and other integrations: an empty device is
        # one nothing points at any more. The shared "standalone containers"
        # device may fall too -- it is recreated the moment one reappears.
        if er.async_entries_for_device(registry, device_id, include_disabled_entities=True):
            continue
        devices.async_remove_device(device_id)


def can_remove_device(entry: UnraidConfigEntry, device: dr.DeviceEntry) -> bool:
    """Whether the user may delete this device from the Home Assistant UI.

    Allowed exactly when the current snapshot accounts for none of the
    device's identifiers -- which is the manual way past the grace period.
    Refused while the relevant section failed, because a snapshot that could
    not read `docker` is not evidence that a container is gone. The server
    device is never removable: deleting it would take the whole entry's
    top-level entities with it and nothing would bring them back but a reload.
    """
    snapshot = entry.runtime_data.coordinator.data
    if snapshot is None:
        return False
    ours = [identifier for domain, identifier in device.identifiers if domain == DOMAIN]
    if not ours:
        return False
    expected = {_identifier(entry, suffix) for suffix in expected_device_suffixes(snapshot)}
    for identifier in ours:
        if (DOMAIN, identifier) in expected:
            return False
        sections = _sections(_DEVICE_SECTIONS, identifier.removeprefix(entry.entry_id + "_"))
        if sections is None or sections & snapshot.failed:
            return False
    return True
