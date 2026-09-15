"""Unraid SSH — read and control an Unraid server over SSH, no Connect API.

One config entry = one server. Setup builds the SSH client and the fast
coordinator, runs the first poll (a failure here makes HA retry the entry
instead of loading half of it) and forwards the platforms. The slow update
coordinator starts in the background -- its first run takes minutes.
"""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN
from .coordinator import (
    UnraidConfigEntry,
    UnraidCoordinator,
    UnraidRuntime,
    UpdateCoordinator,
    build_client,
)
from .icon_cache import ContainerIconCache, async_remove_icon_files, async_setup_icon_http
from .migration import async_reconcile_devices
from .prune import async_prune_stale, can_remove_device
from .ssh import SSHKeyError

_LOGGER = logging.getLogger(__name__)

# Every entry here needs its module: forwarding a platform whose module does
# not exist makes setup fail.
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry) -> bool:
    try:
        client = build_client(entry)
    except SSHKeyError as err:
        # The key pair lives in the entry itself; no retry and no reauth can
        # repair a stored key that no longer parses.
        raise ConfigEntryError("stored SSH key unreadable; remove and re-add the entry") from err
    coordinator = UnraidCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    updates = UpdateCoordinator(hass, entry, client)
    local_icons = True
    try:
        await async_setup_icon_http(hass)
    except OSError:
        # Pictures are optional. Do not copy files without a working route,
        # and never bypass the cache-directory symlink/permission checks.
        local_icons = False
        _LOGGER.warning("Local container pictures unavailable until reload; using URL or standard icons")
    icons = ContainerIconCache(hass, entry.entry_id, client, local_enabled=local_icons)
    entry.runtime_data = UnraidRuntime(client=client, coordinator=coordinator, updates=updates, icons=icons)

    # Direct device creation precedes platform translation loading.
    await async_get_translations(hass, hass.config.language, "device", {DOMAIN})
    async_reconcile_devices(hass, entry)

    def reconcile() -> None:
        # Order matters: migration first decides where every entity belongs,
        # then the cleanup decides whether it should still exist. The other way
        # round, a device emptied by the cleanup would be recreated at once.
        async_reconcile_devices(hass, entry)
        async_prune_stale(hass, entry)

    entry.async_on_unload(coordinator.async_add_listener(reconcile))

    def schedule_icons() -> None:
        if coordinator.last_update_success and coordinator.data is not None:
            icons.schedule(coordinator.data)

    entry.async_on_unload(coordinator.async_add_listener(schedule_icons))
    schedule_icons()
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await icons.async_shutdown()
        raise
    async_reconcile_devices(hass, entry)
    # The first digest check takes minutes; it must not delay setup or fail it.
    entry.async_create_background_task(hass, updates.async_refresh(), "unraid_ssh first update check")
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: UnraidConfigEntry, device: DeviceEntry
) -> bool:
    """Let the user delete a device the server no longer accounts for.

    Home Assistant only offers the delete button on an integration that
    defines this, so without it the only way out of a stale device is the five
    minute grace period -- or removing the whole entry.
    """
    return can_remove_device(entry, device)


async def async_unload_entry(hass: HomeAssistant, entry: UnraidConfigEntry) -> bool:
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.icons.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: UnraidConfigEntry) -> None:
    try:
        await async_remove_icon_files(hass, entry.entry_id)
    except OSError:
        _LOGGER.warning("Could not remove local container pictures; unsafe or inaccessible directory left untouched")


async def _async_reload(hass: HomeAssistant, entry: UnraidConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
