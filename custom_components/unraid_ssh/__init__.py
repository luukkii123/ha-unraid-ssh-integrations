"""Unraid SSH — read and control an Unraid server over SSH, no Connect API.

One config entry = one server. Setup builds the SSH client and the fast
coordinator, runs the first poll (a failure here makes HA retry the entry
instead of loading half of it) and forwards the platforms. The slow update
coordinator starts in the background -- its first run takes minutes.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .coordinator import (
    UnraidConfigEntry,
    UnraidCoordinator,
    UnraidRuntime,
    UpdateCoordinator,
    build_client,
)
from .ssh import SSHKeyError

# Platform.UPDATE joins this list together with update.py: forwarding a
# platform whose module does not exist makes setup fail.
PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH]


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
    entry.runtime_data = UnraidRuntime(client=client, coordinator=coordinator, updates=updates)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # The first digest check takes minutes; it must not delay setup or fail it.
    entry.async_create_background_task(hass, updates.async_refresh(), "unraid_ssh first update check")
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UnraidConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: UnraidConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
