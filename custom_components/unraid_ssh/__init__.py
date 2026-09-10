"""Unraid SSH — read and control an Unraid server over SSH, no Connect API.

One config entry = one server. Setup builds the SSH client and the fast
coordinator, runs the first poll (a failure here makes HA retry the entry
instead of loading half of it) and forwards the platforms.
"""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .coordinator import UnraidConfigEntry, UnraidCoordinator, UnraidRuntime, build_client
from .ssh import SSHKeyError

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: UnraidConfigEntry) -> bool:
    try:
        client = build_client(entry)
    except SSHKeyError as err:
        # The key pair lives in the entry itself; no retry and no reauth can
        # repair a stored key that no longer parses.
        raise ConfigEntryError("stored SSH key unreadable; remove and re-add the entry") from err
    coordinator = UnraidCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = UnraidRuntime(client=client, coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: UnraidConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload(hass: HomeAssistant, entry: UnraidConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
