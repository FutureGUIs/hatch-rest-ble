"""Device-owned programs, refreshed independently of live output state."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypeVar

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .programs import ProgramConflictError, ProgramSnapshot, RestProgram
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

    async def async_shutdown(self) -> None:
        """Stop this refresh timer and release program work before BLE closes."""
        self._closed = True
        await super().async_shutdown()
        requests = tuple(self._requests)
        for task in requests:
            task.cancel()
        if requests:
            await asyncio.gather(*requests, return_exceptions=True)
