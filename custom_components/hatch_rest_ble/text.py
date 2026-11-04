"""Program name and light color."""

from __future__ import annotations

from dataclasses import replace

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .program_coordinator import HatchRestProgramCoordinator
from .program_entity import HatchRestProgramEntity, async_setup_program_entities
from .programs import ProgramSnapshot


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add name and color text entities for each program device."""

    def build(
        coordinator: HatchRestProgramCoordinator, slot: int, _snapshot: ProgramSnapshot
    ) -> list[Entity]:
        return [
            HatchRestProgramName(coordinator, slot),
            HatchRestProgramColor(coordinator, slot),
        ]

    async_setup_program_entities(entry, async_add_entities, build)


class HatchRestProgramName(HatchRestProgramEntity, TextEntity):
    """Program name as shown in the Hatch app: 1-16 printable ASCII characters."""

    _attr_icon = "mdi:rename"
    _attr_native_min = 1
    _attr_native_max = 16
    _attr_pattern = r"[ -~]{1,16}"

    def __init__(self, coordinator: HatchRestProgramCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "name", "Name")

    @property
    def native_value(self) -> str | None:
        return program.name if (program := self._program) is not None else None

    async def async_set_value(self, value: str) -> None:
        await self._async_edit(lambda program: replace(program, name=value))


class HatchRestProgramColor(HatchRestProgramEntity, TextEntity):
    """Program light color as #RRGGBB; brightness is its own entity."""

    _attr_icon = "mdi:palette"
    _attr_native_min = 6
    _attr_native_max = 7
    _attr_pattern = r"#?[0-9A-Fa-f]{6}"

    def __init__(self, coordinator: HatchRestProgramCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "color", "Color")

    @property
    def native_value(self) -> str | None:
        if (program := self._program) is None:
            return None
        return "#{:02X}{:02X}{:02X}".format(*program.color)

    @property
    def extra_state_attributes(self) -> dict[str, list[int]] | None:
        program = self._program
        return {"rgb_color": list(program.color)} if program is not None else None

    async def async_set_value(self, value: str) -> None:
        digits = value.removeprefix("#")
        try:
            color = tuple(bytes.fromhex(digits))
        except ValueError:
            color = ()
        if len(color) != 3:
            raise HomeAssistantError("Enter the color as #RRGGBB, for example #FF8000")
        await self._async_edit(lambda program: replace(program, color=color))
