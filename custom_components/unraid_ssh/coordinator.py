"""The fast coordinator: one SSH round trip per interval, one Snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .collect import TruncatedOutput, build_state_command, split_output
from .const import (
    CONF_HOST,
    CONF_HOST_KEY,
    CONF_PORT,
    CONF_PRIVATE_KEY,
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    POLL_TIMEOUT,
)
from .model import Snapshot, build_snapshot
from .ssh import SSHAuthError, SSHError, SSHHostKeyError, UnraidSSH

_LOGGER = logging.getLogger(__name__)

# Assignment instead of PEP 695 so the file still parses under Python 3.11.
UnraidConfigEntry = ConfigEntry["UnraidRuntime"]


@dataclass
class UnraidRuntime:
    client: UnraidSSH
    coordinator: "UnraidCoordinator"
    updates: Any = None          # UpdateCoordinator, added in stage 3


def setting(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Options win over data — options are what gets edited later."""
    return entry.options.get(key, entry.data.get(key, default))


def build_client(entry: ConfigEntry) -> UnraidSSH:
    return UnraidSSH(
        entry.data[CONF_HOST],
        int(entry.data.get(CONF_PORT, DEFAULT_PORT)),
        entry.data[CONF_PRIVATE_KEY],
        entry.data[CONF_HOST_KEY],
    )


class UnraidCoordinator(DataUpdateCoordinator[Snapshot]):
    """Polls the state command chain."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: UnraidSSH) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=int(setting(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))),
            config_entry=entry,
        )
        self.entry = entry
        self.client = client
        self.host: str = entry.data[CONF_HOST]
        self._reported_failed: set[str] = set()

    async def _async_update_data(self) -> Snapshot:
        try:
            result = await self.client.run(build_state_command(), timeout=POLL_TIMEOUT)
        except (SSHAuthError, SSHHostKeyError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SSHError as err:
            raise UpdateFailed(f"SSH: {err}") from err
        try:
            sections = split_output(result.stdout)
        except TruncatedOutput as err:
            raise UpdateFailed(f"output truncated ({len(result.stdout)} bytes)") from err
        snapshot = build_snapshot(sections, self.data)
        new_failures = snapshot.failed - self._reported_failed
        if new_failures:
            _LOGGER.warning("unraid_ssh: sections unparsable on %s: %s", self.host, ", ".join(sorted(new_failures)))
        self._reported_failed = set(snapshot.failed)
        return snapshot
