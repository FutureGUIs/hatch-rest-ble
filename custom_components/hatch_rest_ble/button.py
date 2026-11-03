"""Read the Rest's stored programs on demand."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import HatchRestCoordinator
from .entity import HatchRestEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Keep refresh available even when the last program read failed."""
    async_add_entities([HatchRestRefreshPrograms(entry.runtime_data)])


class HatchRestRefreshPrograms(HatchRestEntity, ButtonEntity):
    """Refresh the same table managed in the original Hatch app."""

    _attr_name = "Refresh programs"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: HatchRestCoordinator) -> None:
        super().__init__(coordinator, "refresh_programs")

    async def async_press(self) -> None:
        await self.coordinator.program_coordinator.async_refresh_programs()
