"""Program sound."""

from __future__ import annotations

from dataclasses import replace

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SOUNDS
from .program_coordinator import HatchRestProgramCoordinator
from .program_entity import HatchRestProgramEntity, async_setup_program_entities
from .programs import ProgramSnapshot

_SOUND_IDS = {label: sound for sound, label in SOUNDS.items()}


def _unknown(sound: int) -> str:
    return f"Unknown sound ({sound})"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a sound select for each program device."""

    def build(
        coordinator: HatchRestProgramCoordinator, slot: int, _snapshot: ProgramSnapshot
    ) -> list[Entity]:
        return [HatchRestProgramSound(coordinator, slot)]

    async_setup_program_entities(entry, async_add_entities, build)


class HatchRestProgramSound(HatchRestProgramEntity, SelectEntity):
    """The sound the program plays; "None" is silent."""

    _attr_icon = "mdi:music-note"

    def __init__(self, coordinator: HatchRestProgramCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "sound", "Sound")

    @property
    def options(self) -> list[str]:
        options = list(SOUNDS.values())
        if (program := self._program) is not None and program.sound not in SOUNDS:
            options.append(_unknown(program.sound))  # Keep app-only sounds visible.
        return options

    @property
    def current_option(self) -> str | None:
        if (program := self._program) is None:
            return None
        return SOUNDS.get(program.sound, _unknown(program.sound))

    async def async_select_option(self, option: str) -> None:
        if option == self.current_option:
            return
        if (sound := _SOUND_IDS.get(option)) is None:
            raise HomeAssistantError(f"Unknown Hatch Rest sound: {option}")
        await self._async_edit(lambda program: replace(program, sound=sound))
