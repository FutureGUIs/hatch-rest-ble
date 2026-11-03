"""Enable switches for the Rest's stored weekly programs."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SOUNDS
from .program_coordinator import HatchRestProgramCoordinator
from .programs import ProgramConflictError, RestProgram

_DAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add discovered slots and remove only confirmed-deleted program entities."""
    coordinator = entry.runtime_data.program_coordinator
    added_slots: set[int] = set()
    registry = er.async_get(hass)
    prefix = f"{coordinator.address}:program:"
    registered_slots = {
        int(entity.unique_id[len(prefix) :])
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        if entity.platform == DOMAIN
        and entity.domain == "switch"
        and entity.unique_id.startswith(prefix)
        and entity.unique_id[len(prefix) :].isdigit()
        and 1 <= int(entity.unique_id[len(prefix) :]) <= 10
    }

    @callback
    def update_program_entities() -> None:
        snapshot = coordinator.data
        if not coordinator.last_update_success or snapshot is None:
            return  # An unread table is never evidence that programs were deleted.
        slots = {program.slot for program in snapshot.programs if program.exists}
        for slot in (added_slots | registered_slots) - slots:
            entity_id = registry.async_get_entity_id(
                "switch", DOMAIN, f"{coordinator.address}:program:{slot}"
            )
            if (
                entity_id is not None
                and (entity := registry.async_get(entity_id)) is not None
                and entity.config_entry_id == entry.entry_id
            ):
                registry.async_remove(entity_id)
        additions = slots - added_slots
        added_slots.intersection_update(slots)
        registered_slots.intersection_update(slots)
        added_slots.update(additions)
        if additions:
            async_add_entities(
                HatchRestProgramSwitch(coordinator, slot) for slot in sorted(additions)
            )

    entry.async_on_unload(coordinator.async_add_listener(update_program_entities))
    update_program_entities()


class HatchRestProgramSwitch(
    CoordinatorEntity[HatchRestProgramCoordinator], SwitchEntity
):
    """Arm a stored program; this does not start or stop its current run."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: HatchRestProgramCoordinator, slot: int) -> None:
        super().__init__(coordinator)
        self.slot = slot
        self._attr_unique_id = f"{coordinator.address}:program:{slot}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
        )

    @property
    def _program(self) -> RestProgram | None:
        snapshot = self.coordinator.data
        return snapshot.get(self.slot) if snapshot is not None else None

    @property
    def name(self) -> str:
        program = self._program
        return f"{program.name} program" if program else f"Program {self.slot}"

    @property
    def available(self) -> bool:
        return super().available and self._program is not None

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
            "start_time": program.start.strftime("%H:%M:%S"),
            "duration_seconds": program.duration,
            "calculated_end_time": end.strftime("%H:%M:%S"),
            "ends_next_day": end.date() != program.start.date(),
            "repeat_days": [
                name
                for index, name in enumerate(_DAY_NAMES)
                if program.days & (1 << index)
            ],
            "rgb_color": program.color,
            "brightness": program.brightness,
            "sound": SOUNDS.get(program.sound, f"Unknown sound ({program.sound})"),
            "volume_raw": program.volume,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        try:
            await self.coordinator.async_set_program_enabled(self.slot, enabled)
        except ProgramConflictError as err:
            raise HomeAssistantError(
                "The program changed in the Hatch app; refresh programs before retrying"
            ) from err
