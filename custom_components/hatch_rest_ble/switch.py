"""Program switches: enabled, repeat days, and button lock."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SOUNDS
from .program_coordinator import HatchRestProgramCoordinator
from .program_entity import HatchRestProgramEntity, async_setup_program_entities
from .programs import (
    WEEKDAY_BITS,
    ProgramSnapshot,
    RestProgram,
    with_enabled,
    with_toddler_lock,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add switches for each program device."""

    def build(
        coordinator: HatchRestProgramCoordinator, slot: int, snapshot: ProgramSnapshot
    ) -> list[Entity]:
        entities: list[Entity] = [HatchRestProgramSwitch(coordinator, slot)]
        entities.extend(
            HatchRestProgramDay(coordinator, slot, key, label, bit)
            for key, label, bit in WEEKDAY_BITS
        )
        if snapshot.supports_toddler_lock:
            entities.append(HatchRestProgramLock(coordinator, slot))
        return entities

    async_setup_program_entities(entry, async_add_entities, build)


class HatchRestProgramSwitch(HatchRestProgramEntity, SwitchEntity):
    """Arm a stored program; this does not start or stop its current run."""

    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: HatchRestProgramCoordinator, slot: int) -> None:
        # Unique ID unchanged from 0.2/0.3 so entity IDs and history carry over.
        super().__init__(coordinator, slot, None, "Enabled")

    @property
    def is_on(self) -> bool | None:
        return program.enabled if (program := self._program) is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if (program := self._program) is None:
            return {"slot": self.slot}
        end = program.start + timedelta(seconds=program.duration)
        return {
            "slot": self.slot,
            "program_name": program.name,
            "start_time": program.start.strftime("%H:%M:%S"),
            "duration_seconds": program.duration,
            "calculated_end_time": end.strftime("%H:%M:%S"),
            "ends_next_day": end.date() != program.start.date(),
            # Sunday first, as in 0.2/0.3 (the native mask's bit order).
            "repeat_days": [
                label
                for _key, label, bit in sorted(WEEKDAY_BITS, key=lambda day: day[2])
                if program.days & bit
            ],
            "rgb_color": program.color,
            "brightness": program.brightness,
            "sound": SOUNDS.get(program.sound, f"Unknown sound ({program.sound})"),
            "volume_raw": program.volume,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_edit(lambda program: with_enabled(program, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_edit(lambda program: with_enabled(program, False))


class HatchRestProgramDay(HatchRestProgramEntity, SwitchEntity):
    """Whether the program repeats on one weekday."""

    _attr_icon = "mdi:calendar-week"

    def __init__(
        self,
        coordinator: HatchRestProgramCoordinator,
        slot: int,
        key: str,
        label: str,
        bit: int,
    ) -> None:
        super().__init__(coordinator, slot, key, label)
        self._bit = bit

    @property
    def is_on(self) -> bool | None:
        program = self._program
        return bool(program.days & self._bit) if program is not None else None

    def _set(self, program: RestProgram, on: bool) -> RestProgram:
        days = program.days | self._bit if on else program.days & ~self._bit
        return replace(program, days=days)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_edit(lambda program: self._set(program, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_edit(lambda program: self._set(program, False))


class HatchRestProgramLock(HatchRestProgramEntity, SwitchEntity):
    """Lock the Rest's buttons while this program runs (firmware 1.0.4.31+)."""

    _attr_icon = "mdi:lock"

    def __init__(self, coordinator: HatchRestProgramCoordinator, slot: int) -> None:
        super().__init__(coordinator, slot, "button_lock", "Button lock")

    @property
    def is_on(self) -> bool | None:
        program = self._program
        return program.toddler_lock if program is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_edit(lambda program: with_toddler_lock(program, True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_edit(lambda program: with_toddler_lock(program, False))
