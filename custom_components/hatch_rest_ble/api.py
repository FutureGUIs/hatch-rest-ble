"""Short, serialized GATT sessions routed by Home Assistant Bluetooth."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from time import monotonic
from typing import TYPE_CHECKING, Any, TypeVar

from bleak import BleakError
from bleak.backends.device import BLEDevice
from bleak.exc import BleakGATTProtocolError, BleakGATTProtocolErrorCode
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .const import (
    CACHE_CLEAR_TIMEOUT,
    CHAR_FEEDBACK,
    CHAR_HARDWARE_REVISION,
    CHAR_TX,
    COMMAND_SETTLE_SECONDS,
    DISCONNECT_TIMEOUT,
    GATT_TIMEOUT,
    OPERATION_TIMEOUT,
    RECOVERY_BACKOFF_SECONDS,
    SESSION_TIMEOUT,
)
from .programs import (
    FLAG_ENABLED,
    FLAG_EXISTS,
    PROGRAM_SLOTS,
    TODDLER_LOCK_MASK,
    ProgramConflictError,
    ProgramSnapshot,
    ProgramVerificationError,
    RestProgram,
    delete_commands,
    enabled_commands,
    parse_clock,
    parse_program,
    program_commands,
    supports_toddler_lock,
    validate_program,
)
from .protocol import (
    HatchRestError,
    ProtocolError,
    RestState,
    UnsupportedDeviceError,
    parse_feedback,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
CommandBuilder = Callable[[RestState], list[bytes]]
StateCallback = Callable[[RestState], None]
_INVALID_HANDLE = re.compile(r"\binvalid\s*handle\b", re.IGNORECASE)
CHAR_RX = "02240003-5efd-47eb-9c1a-de53f7a2b232"
CHAR_FIRMWARE_REVISION = "00002a26-0000-1000-8000-00805f9b34fb"
PROGRAM_SESSION_TIMEOUT = 120
PROGRAM_RESPONSE_LIMIT = 4096
PROGRAM_RESPONSE_PACKETS = 128
PROGRAM_COMMAND_LIMIT = 20
PROGRAM_CLOCK_TIMEOUT = 3
_PROGRAM_TERMINALS = frozenset(
    (b"OK", b"E01", b"E02", b"E03", b"E04", b"E05", b"E06", b"Unknown")
)
_ProgramResult = TypeVar("_ProgramResult")


def _is_invalid_handle(error: BleakError) -> bool:
    """Recognize native ATT code 1 and the ESPHome/BlueZ error descriptions."""
    if isinstance(error, BleakGATTProtocolError):
        return error.code == BleakGATTProtocolErrorCode.INVALID_HANDLE
    description = str(error).replace("_", " ").replace("-", " ")
    return _INVALID_HANDLE.search(description) is not None


@dataclass
class _SessionProgress:
    """Track whether repeating an operation could replay a device command."""

    write_started: bool = False


class _SessionClient(BleakClientWithServiceCache):
    """Expose the pending client so a cancelled connection can be cleaned up."""

    def __init__(
        self,
        *args: Any,
        session_callback: Callable[[BleakClientWithServiceCache], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        session_callback(self)


class ProgramCommandError(HatchRestError):
    """The Rest rejected a native-program request at the application layer."""


class _ProgramChannel:
    """Pair one ASCII request with data packets followed by a device status."""

    def __init__(self, client: BleakClientWithServiceCache, tx: Any) -> None:
        self.client = client
        self.tx = tx
        self._pending: asyncio.Future[bytes] | None = None
        self._data = bytearray()
        self._packets = 0

    def notification(self, _sender: Any, value: bytearray) -> None:
        """Keep packet boundaries: only a standalone status ends a response."""
        pending = self._pending
        if pending is None or pending.done():
            return
        packet = bytes(value)
        if packet in _PROGRAM_TERMINALS:
            if packet == b"OK":
                pending.set_result(bytes(self._data))
            else:
                pending.set_exception(
                    ProgramCommandError(
                        "Rest rejected native program command: "
                        f"{packet.decode('ascii')}"
                    )
                )
            return
        self._packets += 1
        if (
            len(self._data) + len(packet) > PROGRAM_RESPONSE_LIMIT
            or self._packets > PROGRAM_RESPONSE_PACKETS
        ):
            pending.set_exception(ProtocolError("Native program response is too large"))
            return
        self._data.extend(packet)

    async def request(
        self,
        command: bytes,
        *,
        progress: _SessionProgress | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """Wait for application ACK, not merely completion of the ATT write."""
        if (
            not isinstance(command, bytes)
            or not 1 <= len(command) <= PROGRAM_COMMAND_LIMIT
        ):
            raise ValueError("Native program commands must fit one 20-byte ATT payload")
        if any(value < 32 or value > 126 for value in command):
            raise ValueError("Native program commands must contain printable ASCII")
        pending = asyncio.get_running_loop().create_future()
        self._pending = pending
        self._data = bytearray()
        self._packets = 0
        try:
            async with asyncio.timeout(GATT_TIMEOUT if timeout is None else timeout):
                if progress is not None:
                    # Set this before the ATT write: even a failed write may have
                    # changed the device, and must never be replayed automatically.
                    progress.write_started = True
                await self.client.write_gatt_char(
                    self.tx, command, response="write" in self.tx.properties
                )
                return await pending
        finally:
            self._pending = None
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                # A write may fail after a notification already reported an
                # error. Retrieve it even when that write error takes precedence.
                pending.exception()


class HatchRestClient:
    """Control one legacy Rest through the HA-selected adapter or active proxy."""

    def __init__(self, hass: HomeAssistant, address: str, name: str) -> None:
        self.hass = hass
        self.address = address
        self.name = name
        self._lock = asyncio.Lock()
        self._client: BleakClientWithServiceCache | None = None
        self._closed = False
        self._hardware_checked = False
        self._hardware_version: str | None = None
        self._force_fresh_services = False

    def _get_device(self) -> BLEDevice:
        # HA owns scanning and can select a different adapter for each session.
        from homeassistant.components.bluetooth import async_ble_device_from_address

        if (
            device := async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
        ) is None:
            raise HatchRestError("No connectable Bluetooth route to this Rest")
        return device

    def _remember_client(self, client: BleakClientWithServiceCache) -> None:
        self._client = client

    async def _disconnect(self) -> None:
        if (client := self._client) is None:
            return
        started = monotonic()
        try:
            await asyncio.wait_for(client.disconnect(), DISCONNECT_TIMEOUT)
        except Exception:
            # A proxy can reset its local connection state even when its
            # disconnect RPC fails. Do not keep a dead client blocking updates.
            if getattr(client, "is_connected", True) is False:
                if self._client is client:
                    self._client = None
                _LOGGER.debug(
                    "%s: Disconnect reported an error after the connection closed",
                    self.address,
                    exc_info=True,
                )
                return
            # Retain ownership of a possibly live connection for later cleanup.
            _LOGGER.warning(
                "%s: Could not release Rest Bluetooth connection; will retry cleanup",
                self.address,
                exc_info=True,
            )
        else:
            if self._client is client:
                self._client = None
        finally:
            _LOGGER.debug(
                "%s: Bluetooth disconnect finished in %.3fs",
                self.address,
                monotonic() - started,
            )

    async def _release_session(self) -> None:
        """Finish bounded cleanup even when the operation is cancelled."""
        cleanup = asyncio.create_task(self._disconnect())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def _invalidate_cache(self) -> None:
        """Invalidate the connected route before disconnecting and rediscovering."""
        self._force_fresh_services = True
        self._hardware_checked = False
        if (client := self._client) is None:
            return
        try:
            async with asyncio.timeout(CACHE_CLEAR_TIMEOUT):
                cleared = await client.clear_cache()
            if not cleared:
                _LOGGER.warning(
                    "%s: Bluetooth cache could not be fully cleared; "
                    "retrying fresh service discovery",
                    self.address,
                )
        except Exception:
            # Preserve the failed operation's error, and still release the slot.
            _LOGGER.warning(
                "%s: Error clearing Bluetooth GATT cache", self.address, exc_info=True
            )

    async def async_update(self) -> RestState:
        """Read device state; every failed read is propagated to the coordinator."""
        return await self.async_control(None)

    async def async_control(
        self,
        build_commands: CommandBuilder | None,
        *,
        on_state: StateCallback | None = None,
    ) -> RestState:
        """Read, derive commands from fresh state, write, read back, and release."""
        started = monotonic()
        try:
            # Bound both the wait for another operation and connector retries.
            async with asyncio.timeout(OPERATION_TIMEOUT):
                async with self._lock:
                    _LOGGER.debug(
                        "%s: Bluetooth operation acquired lock after %.3fs",
                        self.address,
                        monotonic() - started,
                    )
                    return await self._async_control_locked(build_commands, on_state)
        except (BleakError, OSError, TimeoutError) as err:
            raise HatchRestError(f"Bluetooth operation failed: {err}") from err

    async def _async_control_locked(
        self, build_commands: CommandBuilder | None, on_state: StateCallback | None
    ) -> RestState:
        for attempt in range(2):
            if self._closed:
                raise HatchRestError("Rest connection is closed")
            if self._client is not None:
                await self._release_session()
                if self._client is not None:
                    raise HatchRestError(
                        "Previous Bluetooth connection could not be released; "
                        "check the adapter or proxy"
                    )
            progress = _SessionProgress()
            try:
                async with asyncio.timeout(SESSION_TIMEOUT):
                    state = await self._async_session(build_commands, progress)
                self._force_fresh_services = False
                # Publish confirmed feedback before proxy teardown can delay UI
                # updates. Keep the lock until cleanup completes, as before.
                if on_state is not None:
                    on_state(state)
                return state
            except (BleakError, ProtocolError, UnsupportedDeviceError) as err:
                if isinstance(err, BleakError) and not _is_invalid_handle(err):
                    raise
                # ESPHome can still consult its host/remote cache when only
                # use_services_cache=False is set, so clear on the live client.
                await self._invalidate_cache()
                if progress.write_started or attempt:
                    raise  # Never replay a write, even if its outcome is unknown.
                _LOGGER.debug(
                    "%s: Stale or incompatible GATT data; "
                    "reconnecting with fresh services",
                    self.address,
                )
            finally:
                await self._release_session()
            await asyncio.sleep(RECOVERY_BACKOFF_SECONDS)
        raise HatchRestError("Unable to read Rest after service discovery recovery")

    async def _async_session(
        self, build_commands: CommandBuilder | None, progress: _SessionProgress
    ) -> RestState:
        device = self._get_device()
        started = monotonic()
        client = await establish_connection(
            _SessionClient,
            device,
            self.name,
            ble_device_callback=self._get_device,
            session_callback=self._remember_client,
            use_services_cache=not self._force_fresh_services,
            timeout=20,
        )
        self._client = client
        _LOGGER.debug(
            "%s: Bluetooth connected in %.3fs", self.address, monotonic() - started
        )
        tx = client.services.get_characteristic(CHAR_TX)
        feedback = client.services.get_characteristic(CHAR_FEEDBACK)
        if tx is None or feedback is None:
            raise UnsupportedDeviceError(
                "Missing first-generation Rest command/feedback characteristics"
            )
        if "read" not in feedback.properties or not (
            {"write", "write-without-response"} & set(tx.properties)
        ):
            raise UnsupportedDeviceError("Incompatible Rest GATT properties")
        if not self._hardware_checked:
            hardware = client.services.get_characteristic(CHAR_HARDWARE_REVISION)
            if hardware is not None and "read" in hardware.properties:
                async with asyncio.timeout(GATT_TIMEOUT):
                    raw_version = await client.read_gatt_char(hardware)
                self._hardware_version = (
                    bytes(raw_version)
                    .decode("ascii", errors="replace")
                    .strip("\x00 \r\n")
                    or None
                )
            self._hardware_checked = True
        async with asyncio.timeout(GATT_TIMEOUT):
            state = parse_feedback(await client.read_gatt_char(feedback))
        state = self._with_hardware(state)
        _LOGGER.debug(
            "%s: Initial feedback ready after %.3fs",
            self.address,
            monotonic() - started,
        )
        commands = build_commands(state) if build_commands is not None else []
        for command in commands:
            progress.write_started = True
            async with asyncio.timeout(GATT_TIMEOUT):
                await client.write_gatt_char(
                    tx, command, response="write" in tx.properties
                )
            # Legacy firmware needs time to apply consecutive commands.
            await asyncio.sleep(COMMAND_SETTLE_SECONDS)
        if commands:
            async with asyncio.timeout(GATT_TIMEOUT):
                state = parse_feedback(await client.read_gatt_char(feedback))
            _LOGGER.debug(
                "%s: Readback ready after %d commands in %.3fs",
                self.address,
                len(commands),
                monotonic() - started,
            )
        return self._with_hardware(state)

    def _with_hardware(self, state: RestState) -> RestState:
        """Mirror the app's volume scaling for legacy BLE hardware revisions."""
        return replace(
            state,
            hardware_version=self._hardware_version,
            volume_min=8 if (self._hardware_version or "").startswith("2") else 0,
        )

    async def async_get_programs(self) -> ProgramSnapshot:
        """Read every native slot and optional clock metadata without mutations."""

        async def read_all(channel, firmware, _progress, deadline):
            programs = []
            for slot in range(1, PROGRAM_SLOTS + 1):
                program = await self._read_program(channel, slot, firmware)
                if program.exists:
                    programs.append(program)
            device_time = None
            # GT is last: an optional timed-out reply cannot become the ACK for
            # a subsequent slot query in this connection.
            clock_timeout = min(PROGRAM_CLOCK_TIMEOUT, deadline - monotonic() - 0.1)
            if clock_timeout > 0:
                try:
                    device_time = parse_clock(
                        await channel.request(b"GT", timeout=clock_timeout)
                    )
                except (
                    HatchRestError,
                    BleakError,
                    OSError,
                    TimeoutError,
                ):
                    _LOGGER.debug(
                        "%s: Native device clock is unavailable",
                        self.address,
                        exc_info=True,
                    )
            return ProgramSnapshot(
                programs=tuple(programs),
                device_time=device_time,
                firmware_version=firmware,
                volume_min=8 if (self._hardware_version or "").startswith("2") else 0,
            )

        return await self._async_program_operation(read_all)

    async def async_save_program(
        self, program: RestProgram, *, expected: RestProgram | None
    ) -> RestProgram:
        """Compare a fresh slot, stage requested fields, and verify the commit."""
        if not isinstance(program, RestProgram):
            raise ValueError("Expected a native Rest program")
        validate_program(program)
        if expected is not None and (
            not isinstance(expected, RestProgram)
            or expected.slot != program.slot
            or not expected.exists
        ):
            raise ValueError("The expected program must match the edited occupied slot")

        async def save(channel, firmware, progress, _deadline):
            current = await self._read_program(channel, program.slot, firmware)
            if (expected is None and current.exists) or (
                expected is not None and current != expected
            ):
                raise ProgramConflictError(
                    "The native program changed; refresh before saving"
                )
            lock_supported = supports_toddler_lock(firmware)
            buttons = program.buttons
            if not current.exists:
                if not lock_supported:
                    if buttons not in (None, 0):
                        raise ValueError(
                            "Toddler lock requires confirmed supported firmware"
                        )
                    buttons = None
                elif buttons is None:
                    buttons = 0
            elif not lock_supported or current.buttons is None:
                if buttons != current.buttons:
                    raise ValueError(
                        "Toddler lock cannot be changed without supported firmware "
                        "and a confirmed device value"
                    )
            elif buttons is None:
                buttons = current.buttons
            else:
                buttons = (current.buttons & ~TODDLER_LOCK_MASK) | (
                    buttons & TODDLER_LOCK_MASK
                )
            target = replace(
                program,
                flags=(current.flags & ~FLAG_ENABLED)
                | FLAG_EXISTS
                | (program.flags & FLAG_ENABLED)
                if current.exists
                else program.flags,
                buttons=buttons,
                power=current.power if current.exists else program.power,
                presets=current.presets if current.exists else 0,
            )
            commands = program_commands(target, supports_toddler_lock=lock_supported)
            for command in commands:
                await channel.request(command, progress=progress)
            verified = await self._read_program(channel, target.slot, firmware)
            if verified != target:
                raise ProgramVerificationError(
                    "The Rest did not report the saved program; refresh before retrying"
                )
            return verified

        return await self._async_program_operation(save)

    async def async_set_program_enabled(
        self, slot: int, enabled: bool, *, expected: RestProgram | None = None
    ) -> RestProgram:
        """Change only fresh enable flags, preserving all other native fields."""
        self._validate_program_slot(slot)
        if type(enabled) is not bool:
            raise ValueError("Program enabled must be a boolean")
        if expected is not None and (
            not isinstance(expected, RestProgram)
            or expected.slot != slot
            or not expected.exists
        ):
            raise ValueError(
                "The expected program must match the toggled occupied slot"
            )

        async def set_enabled(channel, firmware, progress, _deadline):
            current = await self._read_program(channel, slot, firmware)
            if not current.exists:
                raise ProgramConflictError("The native program no longer exists")
            if expected is not None and current != expected:
                raise ProgramConflictError(
                    "The native program changed; refresh before changing enable state"
                )
            if current.enabled == enabled:
                return current
            target = replace(
                current,
                flags=current.flags | FLAG_ENABLED
                if enabled
                else current.flags & ~FLAG_ENABLED,
            )
            for command in enabled_commands(current, enabled):
                await channel.request(command, progress=progress)
            verified = await self._read_program(channel, slot, firmware)
            if verified != target:
                raise ProgramVerificationError(
                    "The Rest did not confirm the program enable state"
                )
            return verified

        return await self._async_program_operation(set_enabled)

    async def async_delete_program(self, slot: int, *, expected: RestProgram) -> None:
        """Delete one unchanged program, then confirm the device slot is absent."""
        self._validate_program_slot(slot)
        if (
            not isinstance(expected, RestProgram)
            or expected.slot != slot
            or not expected.exists
        ):
            raise ValueError(
                "The expected program must match the deleted occupied slot"
            )

        async def delete(channel, firmware, progress, _deadline):
            current = await self._read_program(channel, slot, firmware)
            if current != expected:
                raise ProgramConflictError(
                    "The native program changed; refresh before deleting"
                )
            for command in delete_commands(slot):
                await channel.request(command, progress=progress)
            if (await self._read_program(channel, slot, firmware)).exists:
                raise ProgramVerificationError("The Rest still reports this program")

        await self._async_program_operation(delete)

    @staticmethod
    def _validate_program_slot(slot: int) -> None:
        if type(slot) is not int or not 1 <= slot <= PROGRAM_SLOTS:
            raise ValueError(
                f"Program slot must be an integer from 1 to {PROGRAM_SLOTS}"
            )

    async def _read_program(
        self, channel: _ProgramChannel, slot: int, firmware: str | None
    ) -> RestProgram:
        responses = {"EGL": await channel.request(f"EGL{slot:02X}".encode("ascii"))}
        flags = responses["EGL"]
        if not re.fullmatch(rb"[0-9A-Fa-f]{1,2}", flags):
            raise ProtocolError("Missing or malformed program flags")
        if int(flags, 16) & FLAG_EXISTS:
            for field in (
                "EGC",
                "EGD",
                "EGI",
                "EGN",
                "EGP",
                "EGT",
                "EGV",
                "EGX",
                "EGW",
            ):
                responses[field] = await channel.request(
                    f"{field}{slot:02X}".encode("ascii")
                )
            if supports_toddler_lock(firmware):
                responses["EGM"] = await channel.request(
                    f"EGM{slot:02X}".encode("ascii")
                )
        return parse_program(slot, responses)

    async def _async_program_operation(
        self,
        operation: Callable[
            [_ProgramChannel, str | None, _SessionProgress, float],
            Awaitable[_ProgramResult],
        ],
    ) -> _ProgramResult:
        try:
            async with asyncio.timeout(OPERATION_TIMEOUT):
                async with self._lock:
                    for attempt in range(2):
                        if self._closed:
                            raise HatchRestError("Rest connection is closed")
                        if self._client is not None:
                            await self._release_session()
                            if self._client is not None:
                                raise HatchRestError(
                                    "Previous Bluetooth connection could not be "
                                    "released"
                                )
                        progress = _SessionProgress()
                        try:
                            deadline = monotonic() + PROGRAM_SESSION_TIMEOUT
                            async with asyncio.timeout(PROGRAM_SESSION_TIMEOUT):
                                result = await self._async_program_session(
                                    operation, progress, deadline
                                )
                            self._force_fresh_services = False
                            return result
                        except (BleakError, UnsupportedDeviceError) as error:
                            if isinstance(error, BleakError) and not _is_invalid_handle(
                                error
                            ):
                                raise
                            await self._invalidate_cache()
                            if progress.write_started or attempt:
                                raise
                        finally:
                            await self._release_session()
                        await asyncio.sleep(RECOVERY_BACKOFF_SECONDS)
        except (BleakError, OSError, TimeoutError) as error:
            raise HatchRestError(f"Native program operation failed: {error}") from error
        raise HatchRestError("Unable to read native programs after cache recovery")

    async def _async_program_session(
        self,
        operation: Callable[
            [_ProgramChannel, str | None, _SessionProgress, float],
            Awaitable[_ProgramResult],
        ],
        progress: _SessionProgress,
        deadline: float,
    ) -> _ProgramResult:
        device = self._get_device()
        client = await establish_connection(
            _SessionClient,
            device,
            self.name,
            ble_device_callback=self._get_device,
            session_callback=self._remember_client,
            use_services_cache=not self._force_fresh_services,
            timeout=20,
        )
        self._client = client
        tx = client.services.get_characteristic(CHAR_TX)
        rx = client.services.get_characteristic(CHAR_RX)
        if (
            tx is None
            or not {"write", "write-without-response"}.intersection(tx.properties)
            or rx is None
            or "notify" not in rx.properties
        ):
            raise UnsupportedDeviceError("Missing native Rest command/response channel")
        if not self._hardware_checked:
            hardware = client.services.get_characteristic(CHAR_HARDWARE_REVISION)
            if hardware is not None and "read" in hardware.properties:
                async with asyncio.timeout(GATT_TIMEOUT):
                    raw_version = await client.read_gatt_char(hardware)
                self._hardware_version = (
                    bytes(raw_version)
                    .decode("ascii", errors="replace")
                    .strip("\x00 \r\n")
                    or None
                )
            self._hardware_checked = True
        firmware = None
        firmware_char = client.services.get_characteristic(CHAR_FIRMWARE_REVISION)
        if firmware_char is not None and "read" in firmware_char.properties:
            try:
                async with asyncio.timeout(GATT_TIMEOUT):
                    raw_firmware = await client.read_gatt_char(firmware_char)
                firmware = (
                    bytes(raw_firmware).decode("ascii").strip("\x00 \r\n") or None
                )
            except (BleakError, OSError, TimeoutError, UnicodeDecodeError):
                _LOGGER.debug(
                    "%s: Native program firmware metadata is unavailable",
                    self.address,
                    exc_info=True,
                )
        channel = _ProgramChannel(client, tx)
        try:
            async with asyncio.timeout(GATT_TIMEOUT):
                await client.start_notify(rx, channel.notification)
            return await operation(channel, firmware, progress, deadline)
        finally:
            await self._stop_program_notifications(client, rx)

    async def _stop_program_notifications(
        self, client: BleakClientWithServiceCache, rx: Any
    ) -> None:
        async def stop() -> None:
            try:
                async with asyncio.timeout(GATT_TIMEOUT):
                    await client.stop_notify(rx)
            except Exception:
                _LOGGER.debug(
                    "%s: Could not stop native program notifications",
                    self.address,
                    exc_info=True,
                )

        cleanup = asyncio.create_task(stop())
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise

    async def async_close(self) -> None:
        """Prevent new sessions and finish releasing the current connection."""
        self._closed = True
        async with self._lock:
            await self._release_session()
