"""Program duration, brightness, and volume."""

from __future__ import annotations

from dataclasses import replace

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .program_coordinator import HatchRestProgramCoordinator
from .program_entity import HatchRestProgramEntity, async_setup_program_entities
from .programs import (
    MAX_DURATION,
    MIN_DURATION,
    ProgramSnapshot,
    RestProgram,
    brightness_percent,
    brightness_raw,
    volume_percent,
    volume_raw,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add number entities for each program device."""

    def build(
        coordinator: HatchRestProgramCoordinator, slot: int, _snapshot: ProgramSnapshot
    ) -> list[Entity]:
        return [
            HatchRestProgramDuration(coordinator, slot),
            HatchRestProgramBrightness(coordinator, slot),
            HatchRestProgramVolume(coordinator, slot),
        ]

    async_setup_program_entities(entry, async_add_entities, build)


class HatchRestProgramDuration(HatchRestProgramEntity, NumberEntity):
    """How long the program runs, in minutes (up to 17 h 59 min)."""

    _attr_device_class = NumberDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = MIN_DURATION / 60
    _attr_native_max_value = MAX_DURATION / 60
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: HatchRestProgramCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "duration", "Duration")

    @property
    def native_value(self) -> int | None:
        program = self._program
        # Whole minutes, as in the Hatch app's hours/minutes picker. Stored
        # durations can carry extra seconds (often 30); those are kept below.
        return program.duration // 60 if program is not None else None

    async def async_set_native_value(self, value: float) -> None:
        minutes = int(round(value))

        def change(program: RestProgram) -> RestProgram:
            if minutes == program.duration // 60:
                return program  # Unchanged minutes keep the stored seconds.
            return replace(program, duration=minutes * 60)

        await self._async_edit(change)


class HatchRestProgramBrightness(HatchRestProgramEntity, NumberEntity):
    """Program light brightness."""

    _attr_icon = "mdi:brightness-6"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: HatchRestProgramCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "brightness", "Brightness")

    @property
    def native_value(self) -> int | None:
        program = self._program
        return brightness_percent(program.brightness) if program is not None else None

    async def async_set_native_value(self, value: float) -> None:
        percent = int(round(value))
        await self._async_edit(
            lambda program: replace(
                program, brightness=brightness_raw(percent, program.brightness)
            )
        )


class HatchRestProgramVolume(HatchRestProgramEntity, NumberEntity):
    """Program sound volume, scaled like the Hatch app for each hardware revision."""

    _attr_icon = "mdi:volume-high"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: HatchRestProgramCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "volume", "Volume")

    def _minimum(self) -> int:
        snapshot = self.coordinator.data
        return snapshot.volume_min if snapshot is not None else 0

    @property
    def native_value(self) -> int | None:
        program = self._program
        if program is None:
            return None
        return volume_percent(program.volume, self._minimum())

    async def async_set_native_value(self, value: float) -> None:
        percent = int(round(value))
        minimum = self._minimum()
        await self._async_edit(
            lambda program: replace(
                program, volume=volume_raw(percent, program.volume, minimum)
            )
        )
