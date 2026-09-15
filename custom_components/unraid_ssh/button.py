"""One button: check container images for updates now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import UnraidConfigEntry
from .entity import UnraidEntity, server_device

CHECK = ButtonEntityDescription(key="check_updates")


def expected_keys() -> set[str]:
    """The one button exists for as long as the entry does."""
    return {CHECK.key}


class CheckUpdatesButton(UnraidEntity, ButtonEntity):
    """Runs the slow update coordinator on demand."""

    async def async_press(self) -> None:
        updates = self.coordinator.entry.runtime_data.updates
        await updates.async_check_now()
        if not updates.last_update_success:
            raise HomeAssistantError(f"unraid_ssh: update check failed: {updates.last_exception}")


async def async_setup_entry(
    hass: HomeAssistant, entry: UnraidConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities([CheckUpdatesButton(coordinator, CHECK, server_device(coordinator), "check_updates")])
