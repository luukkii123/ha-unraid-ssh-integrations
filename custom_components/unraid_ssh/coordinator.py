"""Two coordinators.

The fast one: one SSH round trip per interval, one Snapshot.
The slow one: the container image inventory plus one registry lookup per
unique image -- minutes, not seconds, so it runs on its own schedule.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .collect import (
    TruncatedOutput,
    build_inventory_command,
    build_remote_digest_command,
    build_state_command,
    split_output,
)
from .const import (
    CONF_HOST,
    CONF_HOST_KEY,
    CONF_PORT,
    CONF_PRIVATE_KEY,
    CONF_SCAN_INTERVAL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    POLL_TIMEOUT,
    UPDATE_TIMEOUT,
)
from .model import ImageStatus, Snapshot, build_snapshot, build_update_state, refs_to_check
from .parse import parse_image_digests, parse_inventory, parse_remote_digests
from .ssh import SSHAuthError, SSHError, SSHHostKeyError, UnraidSSH

if TYPE_CHECKING:
    from .icon_cache import ContainerIconCache

_LOGGER = logging.getLogger(__name__)

# Assignment instead of PEP 695 so the file still parses under Python 3.11.
UnraidConfigEntry = ConfigEntry["UnraidRuntime"]


@dataclass
class UnraidRuntime:
    client: UnraidSSH
    coordinator: "UnraidCoordinator"
    updates: "UpdateCoordinator"                 # always set in async_setup_entry
    icons: "ContainerIconCache"
    server_device_id: str | None = None
    device_infos: dict[str, dict[str, Any]] = field(default_factory=dict)


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


@dataclass
class UpdateState:
    images: dict[str, ImageStatus]
    checked_at: datetime | None


class UpdateCoordinator(DataUpdateCoordinator[UpdateState]):
    """Slow: inventory, then one registry lookup per unique image, sequentially."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: UnraidSSH) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} updates {entry.title}",
            update_interval=timedelta(hours=int(setting(entry, CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))),
            config_entry=entry,
        )
        self.entry = entry
        self.client = client
        self._lock = asyncio.Lock()

    async def async_check_now(self) -> None:
        """Button: run a check; if one is running, wait for it instead of starting another."""
        if self._lock.locked():
            async with self._lock:
                return
        await self.async_refresh()

    async def _async_update_data(self) -> UpdateState:
        async with self._lock:
            try:
                inventory_result = await self.client.run(build_inventory_command(), timeout=POLL_TIMEOUT * 3)
                sections = split_output(inventory_result.stdout)
                if not {"containers", "images"} <= sections.keys():
                    raise SSHError("incomplete update inventory")
                inventory = parse_inventory(sections.get("containers", ""))
                digests = parse_image_digests(sections.get("images", ""))
                refs = refs_to_check(inventory, digests)
                remote_result = await self.client.run(build_remote_digest_command(refs), timeout=UPDATE_TIMEOUT)
                remote = parse_remote_digests(split_output(remote_result.stdout).get("remote", ""))
            except (SSHAuthError, SSHHostKeyError) as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except (SSHError, TruncatedOutput) as err:
                raise UpdateFailed(f"update check: {err}") from err
            images = build_update_state(inventory, digests, remote)
            unknown = sorted(name for name, s in images.items() if s.remote_digest is None)
            if unknown:
                _LOGGER.info("unraid_ssh: no registry answer for %d image(s): %s", len(unknown), ", ".join(unknown[:10]))
            return UpdateState(images=images, checked_at=dt_util.utcnow())
