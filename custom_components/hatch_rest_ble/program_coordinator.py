"""Device-owned programs, refreshed independently of live output state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .programs import (
    PROGRAM_SLOTS,
    ProgramConflictError,
    ProgramSnapshot,
    RestProgram,
    new_program,
    program_slot_from_key,
    validate_program,
)
from .protocol import HatchRestError

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import HatchRestCoordinator

_LOGGER = logging.getLogger(__name__)
_Result = TypeVar("_Result")
# Programs change rarely and a refresh is ~100 acknowledged round trips; edits
# in HA refresh immediately and the Refresh button covers app-side edits.
PROGRAM_REFRESH_INTERVAL = timedelta(hours=1)
# Edits to one program within this window are saved as one verified write, so
# toggling several days or nudging a slider is one session, not one each.
EDIT_DEBOUNCE_SECONDS = 1.0
ProgramChange = Callable[[RestProgram], RestProgram]


@dataclass
class _PendingEdit:
    """Changes to one slot waiting to be written together."""

    done: asyncio.Future[None]
    changes: list[ProgramChange] = field(default_factory=list)
    timer: asyncio.TimerHandle | None = None


class HatchRestProgramCoordinator(DataUpdateCoordinator[ProgramSnapshot]):
    """Cache confirmed programs without making HA their scheduling engine."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: HatchRestCoordinator,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{device.device_name} programs",
            config_entry=entry,
            update_interval=PROGRAM_REFRESH_INTERVAL,
            always_update=False,
        )
        self.entry = entry
        self.device = device
        self.address = device.address
        self.device_name = device.device_name
        self.client = device.client
        self.data: ProgramSnapshot | None = None
        self._program_lock = asyncio.Lock()
        self._requests: set[asyncio.Task[Any]] = set()
        self._pending: dict[int, _PendingEdit] = {}
        # Set at entry setup, before any program device is created.
        self.hub_device_id: str = ""
        self._closed = False

    async def _run_request(self, operation: Coroutine[Any, Any, _Result]) -> _Result:
        """Own API work so unload can cancel a pending program transaction."""
        if self._closed:
            operation.close()
            raise HatchRestError("Program manager is closed")
        task = self.entry.async_create_background_task(
            self.hass, operation, "Hatch Rest program operation"
        )
        self._requests.add(task)
        try:
            return await task
        finally:
            self._requests.discard(task)

    async def _async_update_data(self) -> ProgramSnapshot:
        try:
            async with self._program_lock:
                return await self._run_request(self.client.async_get_programs())
        except HatchRestError as err:
            raise UpdateFailed(str(err)) from err

    async def async_refresh_programs(self) -> None:
        """Refresh the full table and report failures to the button or editor."""
        await self.async_refresh()
        if not self.last_update_success or self.data is None:
            raise HomeAssistantError(
                f"Could not read Hatch Rest programs: {self.last_exception}"
            )

    def _publish_program(self, program: RestProgram | None, slot: int) -> None:
        """Replace only a verified slot; retain all other device-reported data."""
        snapshot = self.data
        if snapshot is None:
            # Writes are offered only after a complete table read. Do not
            # manufacture a supposedly complete table if lifecycle state changed.
            raise HatchRestError("Refresh programs before changing a program")
        programs = [item for item in snapshot.programs if item.slot != slot]
        if program is not None:
            programs.append(program)
        self.async_set_updated_data(
            replace(
                snapshot, programs=tuple(sorted(programs, key=lambda item: item.slot))
            )
        )

    def _write_failed(self, err: Exception) -> None:
        """Do not publish requested state after a failed or uncertain write."""
        self.async_set_update_error(UpdateFailed(str(err)))

    async def async_save_program(
        self, program: RestProgram, *, expected: RestProgram | None
    ) -> None:
        """Save against a device-read baseline, then publish verified readback."""
        try:
            async with self._program_lock:
                if self.data is None:
                    raise HatchRestError("Refresh programs before saving")
                confirmed = await self._run_request(
                    self.client.async_save_program(program, expected=expected)
                )
                self._publish_program(confirmed, program.slot)
        except ProgramConflictError as err:
            self._write_failed(err)
            raise
        except (HatchRestError, ValueError) as err:
            self._write_failed(err)
            raise HomeAssistantError(
                f"Could not save Hatch Rest program: {err}"
            ) from err

    async def async_set_program_enabled(
        self, slot: int, enabled: bool, *, expected: RestProgram | None = None
    ) -> None:
        """Change an unchanged program's enable bit and publish verified values."""
        try:
            snapshot = self.data
            if snapshot is None:
                raise HatchRestError("Refresh programs before changing a program")
            # Retain the record the user saw even if a queued table refresh
            # discovers a replacement before this request acquires the lock.
            baseline = expected if expected is not None else snapshot.get(slot)
            if baseline is None or not baseline.exists:
                raise ProgramConflictError(
                    "The native program no longer exists; refresh programs"
                )
            async with self._program_lock:
                confirmed = await self._run_request(
                    self.client.async_set_program_enabled(
                        slot, enabled, expected=baseline
                    )
                )
                self._publish_program(confirmed, slot)
        except ProgramConflictError as err:
            self._write_failed(err)
            raise
        except (HatchRestError, ValueError) as err:
            self._write_failed(err)
            raise HomeAssistantError(
                f"Could not change Hatch Rest program: {err}"
            ) from err

    async def async_delete_program(self, slot: int, *, expected: RestProgram) -> None:
        """Delete only the chosen unchanged slot, after verifying it is empty."""
        try:
            async with self._program_lock:
                if self.data is None:
                    raise HatchRestError("Refresh programs before deleting")
                await self._run_request(
                    self.client.async_delete_program(slot, expected=expected)
                )
                self._publish_program(None, slot)
        except ProgramConflictError as err:
            self._write_failed(err)
            raise
        except (HatchRestError, ValueError) as err:
            self._write_failed(err)
            raise HomeAssistantError(
                f"Could not delete Hatch Rest program: {err}"
            ) from err

    # ------------------------------------------------------------------
    # Per-field edits from program device entities

    async def async_edit_program(self, slot: int, change: ProgramChange) -> None:
        """Apply one field change, batched with others to the same slot.

        Returns once the device has confirmed the combined write, so entity
        state only ever shows verified values.
        """
        if self._closed:
            raise HomeAssistantError("The Hatch Rest is unloading")
        current = self.data.get(slot) if self.data is not None else None
        if current is None:
            raise HomeAssistantError(
                f"Program {slot} is no longer on the Rest; refresh programs"
            )
        try:
            validate_program(change(current))  # Reject bad input before queueing.
        except ValueError as err:
            raise HomeAssistantError(f"Invalid program setting: {err}") from err
        pending = self._pending.get(slot)
        if pending is None:
            loop = asyncio.get_running_loop()
            pending = _PendingEdit(done=loop.create_future())
            # Retrieve the outcome even if every waiting caller was cancelled.
            pending.done.add_done_callback(
                lambda done: done.cancelled() or done.exception()
            )
            pending.timer = loop.call_later(
                EDIT_DEBOUNCE_SECONDS, self._start_flush, slot
            )
            self._pending[slot] = pending
        pending.changes.append(change)
        await asyncio.shield(pending.done)

    @callback
    def _start_flush(self, slot: int) -> None:
        if (pending := self._pending.pop(slot, None)) is None:
            return
        self.entry.async_create_background_task(
            self.hass, self._flush(slot, pending), "Save Hatch Rest program"
        )

    async def _flush(self, slot: int, pending: _PendingEdit) -> None:
        try:
            await self._write_changes(slot, pending.changes)
        except ProgramConflictError:
            # The device copy changed, usually in the Hatch app. Load it so
            # the entities show what is really stored, then ask for a retry.
            self.entry.async_create_background_task(
                self.hass, self.async_refresh(), "Read Hatch Rest programs"
            )
            error: Exception = HomeAssistantError(
                f"Program {slot} was changed on the Rest, for example in the "
                "Hatch app. The latest version is being loaded; make the "
                "change again."
            )
        except HomeAssistantError as err:
            error = err
        except asyncio.CancelledError:
            if not pending.done.done():
                pending.done.set_exception(
                    HomeAssistantError("The Hatch Rest is unloading")
                )
            raise
        else:
            if not pending.done.done():
                pending.done.set_result(None)
            return
        if not pending.done.done():
            pending.done.set_exception(error)

    async def _write_changes(self, slot: int, changes: list[ProgramChange]) -> None:
        """Apply queued changes to the record current when the lock is acquired."""
        try:
            async with self._program_lock:
                baseline = self.data.get(slot) if self.data is not None else None
                if baseline is None:
                    raise HomeAssistantError(
                        f"Program {slot} is no longer on the Rest; refresh programs"
                    )
                program = baseline
                for change in changes:
                    program = change(program)
                if program == baseline:
                    return
                if replace(program, flags=baseline.flags) == baseline:
                    # Only the enable bit changed: send the app's minimal toggle.
                    confirmed = await self._run_request(
                        self.client.async_set_program_enabled(
                            slot, program.enabled, expected=baseline
                        )
                    )
                else:
                    confirmed = await self._run_request(
                        self.client.async_save_program(program, expected=baseline)
                    )
                self._publish_program(confirmed, slot)
        except ProgramConflictError as err:
            self._write_failed(err)
            raise
        except (HatchRestError, ValueError) as err:
            self._write_failed(err)
            raise HomeAssistantError(
                f"Could not save Hatch Rest program: {err}"
            ) from err

    async def async_add_program(self) -> int:
        """Create a default program in the first free slot; returns the slot."""
        snapshot = self.data
        if snapshot is None:
            raise HomeAssistantError(
                "Programs have not been read from the Rest yet; press Refresh programs"
            )
        occupied = {program.slot for program in snapshot.programs if program.exists}
        slot = next(
            (slot for slot in range(1, PROGRAM_SLOTS + 1) if slot not in occupied),
            None,
        )
        if slot is None:
            raise HomeAssistantError(
                f"All {PROGRAM_SLOTS} program slots on the Rest are in use; "
                "delete a program device first"
            )
        state = self.device.data
        program = new_program(
            slot,
            today=snapshot.device_time or dt_util.now().replace(tzinfo=None),
            color=state.color,
            brightness=state.brightness,
            sound=state.sound,
            volume=state.volume,
            supports_lock=snapshot.supports_toddler_lock,
        )
        try:
            await self.async_save_program(program, expected=None)
        except ProgramConflictError as err:
            self.entry.async_create_background_task(
                self.hass, self.async_refresh(), "Read Hatch Rest programs"
            )
            raise HomeAssistantError(
                f"Slot {slot} is already used on the Rest; programs are being "
                "reloaded. Try again."
            ) from err
        return slot

    @callback
    def async_sync_registry(self) -> None:
        """Remove devices and entities of programs gone from a complete read."""
        snapshot = self.data
        if not self.last_update_success or snapshot is None:
            return  # An unread table is never evidence that programs were deleted.
        slots = {program.slot for program in snapshot.programs if program.exists}
        devices = dr.async_get(self.hass)
        for device in dr.async_child_entries_for_config_entry(
            devices, self.entry.entry_id
        ):
            device_slots = {
                program_slot_from_key(self.address, value)
                for domain, value in device.identifiers
                if domain == DOMAIN
            } - {None}
            if device_slots and not device_slots & slots:
                devices.async_remove_device(device.id)
        entities = er.async_get(self.hass)
        for entity in er.async_entries_for_config_entry(entities, self.entry.entry_id):
            slot = program_slot_from_key(self.address, entity.unique_id)
            if slot is not None and slot not in slots:
                entities.async_remove(entity.entity_id)

    async def async_shutdown(self) -> None:
        """Stop this refresh timer and release program work before BLE closes."""
        self._closed = True
        for pending in self._pending.values():
            if pending.timer is not None:
                pending.timer.cancel()
            if not pending.done.done():
                pending.done.set_exception(
                    HomeAssistantError("The Hatch Rest is unloading")
                )
        self._pending.clear()
        await super().async_shutdown()
        requests = tuple(self._requests)
        for task in requests:
            task.cancel()
        if requests:
            await asyncio.gather(*requests, return_exceptions=True)
