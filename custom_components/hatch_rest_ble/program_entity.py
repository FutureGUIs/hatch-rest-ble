"""One HA device per native program, with an entity for each program setting."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import ChildDeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .program_coordinator import HatchRestProgramCoordinator, ProgramChange
from .programs import ProgramSnapshot, RestProgram, program_device_key

EntityFactory = Callable[
    [HatchRestProgramCoordinator, int, ProgramSnapshot], Iterable[Entity]
]


@callback
def async_setup_program_entities(
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    factory: EntityFactory,
) -> None:
    """Add a platform's entities for each program found by a complete read.

    Removal is not handled here: the program coordinator removes the device
    (and with it these entities) once a complete read no longer has the slot.
    """
    coordinator: HatchRestProgramCoordinator = entry.runtime_data.program_coordinator
    added: set[int] = set()

    @callback
    def update() -> None:
        snapshot = coordinator.data
        if not coordinator.last_update_success or snapshot is None:
            return
        slots = {program.slot for program in snapshot.programs if program.exists}
        added.intersection_update(slots)
        new_slots = sorted(slots - added)
        added.update(new_slots)
        entities = [
            entity
            for slot in new_slots
            for entity in factory(coordinator, slot, snapshot)
        ]
        if entities:
            async_add_entities(entities)

    entry.async_on_unload(coordinator.async_add_listener(update))
    update()


class HatchRestProgramEntity(CoordinatorEntity[HatchRestProgramCoordinator]):
    """A setting of one native program, shown on that program's device.

    State is always the device-confirmed record. A change is queued with the
    program coordinator, which batches changes made within a second and
    writes and verifies them together.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HatchRestProgramCoordinator,
        slot: int,
        key: str | None,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self.slot = slot
        device_key = program_device_key(coordinator.address, slot)
        self._attr_unique_id = device_key if key is None else f"{device_key}:{key}"
        self._attr_name = name
        # A program is a logical part of the Rest, so it is a child device:
        # it shares the Rest's area and disabled state and is removed with it.
        self._attr_device_info = ChildDeviceInfo(
            identifiers={(DOMAIN, device_key)},
            parent_device_id=coordinator.hub_device_id,
            # Numbered so the 10-slot limit stays visible; the program's own
            # name is its Name entity, so renaming never changes entity IDs.
            name=f"{coordinator.device_name} program {slot}",
        )

    @property
    def _program(self) -> RestProgram | None:
        snapshot = self.coordinator.data
        return snapshot.get(self.slot) if snapshot is not None else None

    @property
    def available(self) -> bool:
        return super().available and self._program is not None

    async def _async_edit(self, change: ProgramChange) -> None:
        await self.coordinator.async_edit_program(self.slot, change)
