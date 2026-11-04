"""Program start time."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
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
    """Add a start time entity for each program device."""

    def build(
        coordinator: HatchRestProgramCoordinator, slot: int, _snapshot: ProgramSnapshot
    ) -> list[Entity]:
        return [HatchRestProgramStart(coordinator, slot)]

    async_setup_program_entities(entry, async_add_entities, build)


class HatchRestProgramStart(HatchRestProgramEntity, TimeEntity):
    """Local wall-clock start time on the Rest's own clock."""

    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator: HatchRestProgramCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "start", "Start time")

    @property
    def native_value(self) -> time | None:
        return program.start.time() if (program := self._program) else None

    async def async_set_value(self, value: time) -> None:
        start = value.replace(microsecond=0, tzinfo=None)
        await self._async_edit(
            lambda program: replace(
                program, start=datetime.combine(program.start.date(), start)
            )
        )
