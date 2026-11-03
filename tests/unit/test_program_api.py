"""Native-program transactions over the real notification/ACK transport."""

import asyncio
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from _rest_unit import api
from _rest_unit.const import CHAR_FEEDBACK, CHAR_HARDWARE_REVISION, CHAR_TX
from _rest_unit.programs import (
    ProgramConflictError,
    ProgramVerificationError,
    RestProgram,
    format_clock,
    parse_clock,
    parse_program,
)
from _rest_unit.protocol import HatchRestError, ProtocolError, UnsupportedDeviceError
from bleak import BleakError


def program(slot=1, **changes):
    """Use nontrivial, representable values to detect unintended field changes."""
    return replace(
        RestProgram(
            slot=slot,
            name="Bedtime",
            start=datetime(2026, 10, 15, 19, 30, 0),
            duration=36030,
            days=0x7F,
            color=(17, 34, 51),
            brightness=112,
            sound=3,
            volume=96,
            flags=0xC4,
            buttons=0x200,
            power=255,
            presets=0x87654321,
        ),
        **changes,
    )


async def wait_for_command(client):
    """Fail promptly if setup fails before the request under observation."""
    async with asyncio.timeout(1):
        while not client.commands:
            await asyncio.sleep(0)


class NativeClient:
    """An ACK-driven firmware model, distinct from ordinary state feedback."""

    def __init__(self, programs=(), *, firmware=b"1.0.4.31"):
        self.programs = {item.slot: item for item in programs}
        self.firmware = firmware
        self.clock = b"20261015193104"
        self.tx = SimpleNamespace(properties=["write"])
        self.rx = SimpleNamespace(properties=["notify"])
        self.hardware = SimpleNamespace(properties=["read"])
        self.firmware_char = SimpleNamespace(properties=["read"])
        self.feedback = SimpleNamespace(properties=["read"])
        self.characteristics = {
            CHAR_TX: self.tx,
            api.CHAR_RX: self.rx,
            CHAR_HARDWARE_REVISION: self.hardware,
            api.CHAR_FIRMWARE_REVISION: self.firmware_char,
            CHAR_FEEDBACK: self.feedback,
        }
        self.services = SimpleNamespace(get_characteristic=self.characteristics.get)
        self.events = []
        self.callback = None
        self.fragment_size = 3
        self.responses = {}
        self.errors = {}
        self.withhold = set()
        self.after_commit = None
        self.selected = None
        self.staged = None
        self.is_connected = True
        self.read_gatt_char = AsyncMock(side_effect=self._read)
        self.write_gatt_char = AsyncMock(side_effect=self._write)
        self.start_notify = AsyncMock(side_effect=self._start)
        self.stop_notify = AsyncMock(side_effect=self._stop)
        self.disconnect = AsyncMock(side_effect=self._disconnect)
        self.clear_cache = AsyncMock(return_value=True)

    async def _start(self, characteristic, callback):
        assert characteristic is self.rx
        self.events.append("notify")
        self.callback = callback

    async def _stop(self, characteristic):
        assert characteristic is self.rx
        self.events.append("stop")
        self.callback = None

    async def _disconnect(self):
        self.events.append("disconnect")

    async def _read(self, characteristic):
        if characteristic is self.hardware:
            return b"2.0"
        if characteristic is self.firmware_char:
            if isinstance(self.firmware, Exception):
                raise self.firmware
            return self.firmware
        if characteristic is self.feedback:
            return b"C\x11\x22\x33\x70S\x03\x60P\0"
        raise AssertionError("Unexpected GATT read")

    def emit(self, packet):
        assert self.callback is not None
        self.callback(self.rx, bytearray(packet))

    def reply(self, data=b"", status=b"OK"):
        for index in range(0, len(data), self.fragment_size):
            self.emit(data[index : index + self.fragment_size])
        self.emit(status)

    def _field(self, command):
        field, slot = command[:3].decode(), int(command[3:], 16)
        item = self.programs.get(slot)
        if field == "EGL":
            return f"{item.flags if item else 0:02X}".encode()
        assert item is not None, "Absent slots must not be queried for fields"
        values = {
            "EGC": "".join(f"{value:02X}" for value in (*item.color, item.brightness)),
            "EGD": f"{item.duration:04X}",
            "EGI": f"{item.power:02X}",
            "EGM": f"{item.buttons or 0:04X}",
            "EGN": f"{item.sound:02X}",
            "EGP": f"{item.presets:08X}",
            "EGT": format_clock(item.start),
            "EGV": f"{item.volume:02X}",
            "EGX": item.name,
            "EGW": f"{item.days:02X}",
        }
        return values[field].encode("utf-8")

    async def _write(self, characteristic, command, *, response):
        assert characteristic is self.tx
        assert self.callback is not None, "Subscribe before issuing native requests"
        assert response == ("write" in self.tx.properties)
        self.events.append(command)
        if command in self.errors:
            raise self.errors[command]
        if command in self.withhold:
            return
        if command in self.responses:
            data, status = self.responses[command]
            self.reply(data, status)
            return
        if command == b"GT":
            self.reply(self.clock)
            return
        if command.startswith(b"EG"):
            self.reply(self._field(command))
            return
        if command.startswith(b"ESB"):
            self.selected = int(command[3:], 16)
            self.staged = self.programs.get(
                self.selected,
                program(
                    self.selected,
                    buttons=0,
                    flags=0,
                    power=255,
                    presets=0,
                ),
            )
        elif command == b"ESF":
            if self.staged.exists:
                self.programs[self.selected] = self.staged
            else:
                self.programs.pop(self.selected, None)
            if self.after_commit:
                self.after_commit(self)
        else:
            assert self.staged is not None
            field, value = command[:3], command[3:]
            if field == b"EST":
                changes = {"start": parse_clock(value)}
            elif field == b"ESX":
                changes = {"name": value.decode("ascii")}
            elif field == b"ESC":
                rgba = bytes.fromhex(value.decode())
                changes = {"color": tuple(rgba[:3]), "brightness": rgba[3]}
            elif field == b"ESM":
                assert value[4:] == b"0000"
                changes = {"buttons": int(value[:4], 16)}
            else:
                name = {
                    b"ESD": "duration",
                    b"ESW": "days",
                    b"ESI": "power",
                    b"ESN": "sound",
                    b"ESV": "volume",
                    b"ESL": "flags",
                }[field]
                changes = {name: int(value, 16)}
            self.staged = replace(self.staged, **changes)
        self.reply()

    @property
    def commands(self):
        return [call.args[1] for call in self.write_gatt_char.await_args_list]

    @property
    def mutations(self):
        return [command for command in self.commands if command.startswith(b"ES")]


@pytest.fixture
def native(monkeypatch):
    client = NativeClient([program()])
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    locator = Mock(return_value=device)
    monkeypatch.setattr(api.HatchRestClient, "_get_device", lambda _: locator())
    connect = AsyncMock(return_value=client)
    monkeypatch.setattr(api, "establish_connection", connect)
    monkeypatch.setattr(api, "RECOVERY_BACKOFF_SECONDS", 0)
    rest = api.HatchRestClient(None, device.address, "Rest")
    return rest, client, connect


async def test_read_complete_table_filters_absent_and_preserves_metadata(native):
    rest, client, connect = native
    client.programs[10] = program(10, name="Morning", flags=0x80)
    snapshot = await rest.async_get_programs()
    assert snapshot.programs == (program(), client.programs[10])
    assert snapshot.get(2) is None
    assert snapshot.device_time == datetime(2026, 10, 15, 19, 31, 4)
    assert snapshot.firmware_version == "1.0.4.31"
    assert snapshot.supports_toddler_lock
    assert snapshot.volume_min == 8
    assert [command for command in client.commands if command.startswith(b"EGL")] == [
        f"EGL{slot:02X}".encode() for slot in range(1, 11)
    ]
    assert client.mutations == []
    assert client.commands[-1] == b"GT"
    assert client.events[0] == "notify"
    assert client.events[-2:] == ["stop", "disconnect"]
    connect.assert_awaited_once()


@pytest.mark.parametrize(
    "status", [b"E01", b"E02", b"E03", b"E04", b"E05", b"E06", b"Unknown"]
)
async def test_application_rejection_is_not_an_att_ack(native, status):
    rest, client, connect = native
    client.responses[b"EGL01"] = (b"", status)
    with pytest.raises(api.ProgramCommandError, match=status.decode()):
        await rest.async_get_programs()
    connect.assert_awaited_once()
    assert client.commands == [b"EGL01"]
    assert client.events[-2:] == ["stop", "disconnect"]


async def test_att_completion_waits_for_separate_ack(native):
    rest, client, _ = native
    client.withhold.add(b"EGL01")
    task = asyncio.create_task(rest.async_get_programs())
    await wait_for_command(client)
    await asyncio.sleep(0)
    assert not task.done()
    client.reply(b"C4")
    assert (await task).get(1) == program()


async def test_fragmented_data_keeps_packet_boundaries_without_trimming(native):
    rest, client, _ = native
    client.programs[1] = program(name="  Sleep well  ")
    client.fragment_size = 1
    assert (await rest.async_get_programs()).get(1).name == "  Sleep well  "


@pytest.mark.parametrize("failure", ["missing_ack", "malformed", "overflow", "packets"])
async def test_invalid_response_is_bounded_and_releases_notifications(
    native, monkeypatch, failure
):
    rest, client, connect = native
    monkeypatch.setattr(api, "GATT_TIMEOUT", 0.01)
    if failure == "missing_ack":
        client.withhold.add(b"EGL01")
    elif failure == "malformed":
        client.responses[b"EGL01"] = (b"not hex", b"OK")
    else:
        client.responses[b"EGL01"] = (b"12345678", b"OK")
        monkeypatch.setattr(
            api,
            "PROGRAM_RESPONSE_LIMIT"
            if failure == "overflow"
            else "PROGRAM_RESPONSE_PACKETS",
            1,
        )
    with pytest.raises(HatchRestError):
        await rest.async_get_programs()
    connect.assert_awaited_once()
    assert client.events[-2:] == ["stop", "disconnect"]
    assert not rest._lock.locked()


@pytest.mark.parametrize(
    "firmware", [None, b"1.0.3.0", BleakError("metadata unavailable")]
)
async def test_unknown_or_old_firmware_reads_without_button_queries(native, firmware):
    rest, client, _ = native
    client.firmware = firmware
    if firmware is None:
        del client.characteristics[api.CHAR_FIRMWARE_REVISION]
    snapshot = await rest.async_get_programs()
    assert not snapshot.supports_toddler_lock
    assert snapshot.get(1) == replace(program(), buttons=None)
    assert b"EGM01" not in client.commands


@pytest.mark.parametrize("failure", ["rejected", "timeout", "malformed"])
async def test_optional_clock_failure_does_not_hide_complete_table(
    native, monkeypatch, failure
):
    rest, client, _ = native
    if failure == "rejected":
        client.responses[b"GT"] = (b"", b"Unknown")
    elif failure == "malformed":
        client.clock = b"garbled"
    else:
        monkeypatch.setattr(api, "PROGRAM_CLOCK_TIMEOUT", 0.01)
        client.withhold.add(b"GT")
    snapshot = await rest.async_get_programs()
    assert snapshot.get(1) == program()
    assert snapshot.device_time is None
    assert client.commands[-1] == b"GT"


@pytest.mark.parametrize("buttons", [0, 0x1FF, None])
async def test_create_known_firmware_uses_requested_lock_and_power_ff(native, buttons):
    rest, client, _ = native
    requested = program(2, name="New", buttons=buttons, flags=0xC0, presets=0)
    actual = await rest.async_save_program(requested, expected=None)
    assert actual == replace(requested, buttons=buttons or 0)
    assert b"ESIFF" in client.mutations
    assert f"ESM{buttons or 0:04X}0000".encode() in client.mutations
    assert client.programs[1] == program()
    assert all(len(command) <= 20 for command in client.commands)


@pytest.mark.parametrize("firmware", [None, b"1.0.3.0"])
@pytest.mark.parametrize("buttons", [None, 0])
async def test_create_unknown_or_old_firmware_can_create_without_lock(
    native, firmware, buttons
):
    rest, client, _ = native
    client.firmware = firmware
    if firmware is None:
        del client.characteristics[api.CHAR_FIRMWARE_REVISION]
    requested = program(2, name="New", buttons=buttons, flags=0xC0, presets=0)
    actual = await rest.async_save_program(requested, expected=None)
    assert actual == replace(requested, buttons=None)
    assert b"ESIFF" in client.mutations
    assert not any(command.startswith(b"ESM") for command in client.mutations)


async def test_create_unknown_firmware_rejects_lock_without_mutations(native):
    rest, client, _ = native
    del client.characteristics[api.CHAR_FIRMWARE_REVISION]
    with pytest.raises(ValueError, match="Toddler lock"):
        await rest.async_save_program(program(2, buttons=0x1FF), expected=None)
    assert client.mutations == []


async def test_save_preserves_opaque_fields_and_untouched_slots(native):
    rest, client, _ = native
    client.programs[2] = program(2, name="Other")
    requested = program(
        name="Changed", brightness=0, buttons=0x1FF, flags=0x80, power=0, presets=0
    )
    actual = await rest.async_save_program(requested, expected=program())
    assert actual == replace(
        requested, flags=0x84, buttons=0x3FF, power=255, presets=0x87654321
    )
    assert client.programs[2] == program(2, name="Other")
    assert b"ESB02" not in client.commands
    assert not any(command.startswith(b"ESP") for command in client.commands)
    assert client.mutations[-1] == b"ESF"


@pytest.mark.parametrize("operation", ["create", "save", "delete"])
async def test_fresh_compare_conflicts_do_not_write(native, operation):
    rest, client, _ = native
    if operation == "create":
        call = rest.async_save_program(program(), expected=None)
    elif operation == "save":
        call = rest.async_save_program(
            program(name="Edited"), expected=program(name="Stale")
        )
    else:
        call = rest.async_delete_program(1, expected=program(name="Stale"))
    with pytest.raises(ProgramConflictError):
        await call
    assert client.mutations == []
    assert client.events[-2:] == ["stop", "disconnect"]


async def test_enable_uses_fresh_flags_and_preserves_read_only_unicode_name(native):
    rest, client, _ = native
    client.programs[1] = program(name="Dodo é", flags=0xA4)
    actual = await rest.async_set_program_enabled(1, True)
    assert actual == program(name="Dodo é", flags=0xE4)
    assert client.mutations == [b"ESB01", b"ESLE4", b"ESF"]


async def test_already_enabled_program_needs_no_mutation(native):
    rest, client, _ = native
    assert await rest.async_set_program_enabled(1, True) == program()
    assert client.mutations == []


@pytest.mark.parametrize("enabled", [True, False])
async def test_named_toggle_rejects_replaced_slot_even_when_already_requested(
    native, enabled
):
    rest, client, _ = native
    baseline = program()
    client.programs[1] = program(name="Replacement", flags=0xC4 if enabled else 0x84)
    with pytest.raises(ProgramConflictError, match="changed"):
        await rest.async_set_program_enabled(1, enabled, expected=baseline)
    assert client.mutations == []
    assert client.programs[1].name == "Replacement"


async def test_named_toggle_accepts_unchanged_baseline(native):
    rest, client, _ = native
    actual = await rest.async_set_program_enabled(1, False, expected=program())
    assert actual == program(flags=0x84)
    assert client.mutations == [b"ESB01", b"ESL84", b"ESF"]


@pytest.mark.parametrize("baseline", ["not a program", program(2), program(flags=0)])
async def test_named_toggle_rejects_malformed_baseline_before_connect(native, baseline):
    rest, client, connect = native
    with pytest.raises(ValueError, match="expected program"):
        await rest.async_set_program_enabled(1, False, expected=baseline)
    connect.assert_not_awaited()
    assert client.commands == []


async def test_enable_absent_program_is_a_conflict(native):
    rest, client, _ = native
    with pytest.raises(ProgramConflictError):
        await rest.async_set_program_enabled(2, True)
    assert client.mutations == []


async def test_delete_only_target_and_verify_absence(native):
    rest, client, _ = native
    client.programs[2] = program(2, name="Other")
    await rest.async_delete_program(1, expected=program())
    assert client.mutations == [b"ESB01", b"ESL00", b"ESF"]
    assert client.programs == {2: program(2, name="Other")}
    assert client.commands[-1] == b"EGL01"


@pytest.mark.parametrize("operation", ["save", "enable", "delete"])
async def test_commit_readback_mismatch_is_failure_without_replay(native, operation):
    rest, client, connect = native
    if operation == "delete":
        client.after_commit = lambda model: model.programs.update({1: program()})
        call = rest.async_delete_program(1, expected=program())
    else:

        def alter(model):
            model.programs[1] = replace(model.programs[1], volume=0)

        client.after_commit = alter
        call = (
            rest.async_save_program(program(name="Changed"), expected=program())
            if operation == "save"
            else rest.async_set_program_enabled(1, False)
        )
    with pytest.raises(ProgramVerificationError):
        await call
    assert client.mutations.count(b"ESB01") == 1
    connect.assert_awaited_once()


async def test_invalid_query_handle_recovers_once_before_mutation(native):
    rest, stale, connect = native
    stale.errors[b"EGL01"] = BleakError("Invalid handle")
    fresh = NativeClient([program()])
    connect.side_effect = [stale, fresh]
    actual = await rest.async_set_program_enabled(1, False)
    assert not actual.enabled
    assert stale.mutations == []
    stale.clear_cache.assert_awaited_once()
    assert [call.kwargs["use_services_cache"] for call in connect.await_args_list] == [
        True,
        False,
    ]
    assert stale.events[-2:] == ["stop", "disconnect"]
    assert fresh.mutations == [b"ESB01", b"ESL84", b"ESF"]


@pytest.mark.parametrize("failure", ["begin", "field", "finish", "readback"])
async def test_invalid_handle_after_begin_never_replays_mutation(native, failure):
    rest, client, connect = native
    error = BleakError("Invalid handle")
    if failure == "readback":
        client.after_commit = lambda model: model.errors.update({b"EGL01": error})
    else:
        client.errors[
            {"begin": b"ESB01", "field": b"EST20261015193000", "finish": b"ESF"}[
                failure
            ]
        ] = error
    with pytest.raises(HatchRestError):
        await rest.async_save_program(program(name="Changed"), expected=program())
    connect.assert_awaited_once()
    assert client.mutations.count(b"ESB01") == 1
    client.clear_cache.assert_awaited_once()
    assert rest._force_fresh_services
    assert client.events[-2:] == ["stop", "disconnect"]


async def test_commit_without_application_ack_is_uncertain_and_not_replayed(
    native, monkeypatch
):
    rest, client, connect = native
    monkeypatch.setattr(api, "GATT_TIMEOUT", 0.01)
    original = client._write

    async def no_finish_ack(characteristic, command, **kwargs):
        if command == b"ESF":
            saved_callback = client.callback
            client.callback = lambda *_: None
            try:
                await original(characteristic, command, **kwargs)
            finally:
                client.callback = saved_callback
        else:
            await original(characteristic, command, **kwargs)

    client.write_gatt_char.side_effect = no_finish_ack
    with pytest.raises(HatchRestError):
        await rest.async_set_program_enabled(1, False)
    assert not client.programs[1].enabled
    assert client.mutations == [b"ESB01", b"ESL84", b"ESF"]
    connect.assert_awaited_once()


async def test_cancelled_response_stops_notifications_and_unlocks(native):
    rest, client, connect = native
    client.withhold.add(b"EGL01")
    task = asyncio.create_task(rest.async_get_programs())
    await wait_for_command(client)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.events[-2:] == ["stop", "disconnect"]
    client.withhold.clear()
    assert (await rest.async_get_programs()).get(1) == program()
    assert connect.await_count == 2


async def test_program_read_serializes_with_ordinary_feedback_poll(native):
    rest, client, connect = native
    client.withhold.add(b"EGL01")
    programs = asyncio.create_task(rest.async_get_programs())
    await wait_for_command(client)
    poll = asyncio.create_task(rest.async_update())
    await asyncio.sleep(0)
    connect.assert_awaited_once()
    client.reply(b"C4")
    assert (await programs).get(1) == program()
    assert (await poll).sound == 3
    assert connect.await_count == 2


async def test_closed_client_rejects_program_work(native):
    rest, client, connect = native
    await rest.async_close()
    with pytest.raises(HatchRestError, match="closed"):
        await rest.async_get_programs()
    connect.assert_not_awaited()
    client.start_notify.assert_not_awaited()


@pytest.mark.parametrize("missing", [CHAR_TX, api.CHAR_RX])
async def test_missing_native_channel_gets_one_fresh_discovery(native, missing):
    rest, client, connect = native
    del client.characteristics[missing]
    with pytest.raises(UnsupportedDeviceError):
        await rest.async_get_programs()
    assert connect.await_count == 2
    assert client.disconnect.await_count == 2
    client.start_notify.assert_not_awaited()


async def test_without_response_characteristic_still_needs_device_ack(native):
    rest, client, _ = native
    client.tx.properties = ["write-without-response"]
    assert (await rest.async_get_programs()).get(1) == program()
    assert all(
        not call.kwargs["response"] for call in client.write_gatt_char.await_args_list
    )


async def test_channel_concatenates_binary_data_but_only_standalone_ok_terminates(
    native,
):
    _, client, _ = native
    channel = api._ProgramChannel(client, client.tx)
    client.callback = channel.notification
    client.withhold.add(b"EGB01")
    task = asyncio.create_task(channel.request(b"EGB01"))
    await asyncio.sleep(0)
    client.emit(b"\x00OK\xff")
    await asyncio.sleep(0)
    assert not task.done()
    client.emit(b"OK")
    assert await task == b"\x00OK\xff"


@pytest.mark.parametrize("command", [b"X" * 21, b"", b"ESX\xff", b"ESX\x00"])
async def test_channel_rejects_unsafe_outbound_commands_before_att(native, command):
    _, client, _ = native
    channel = api._ProgramChannel(client, client.tx)
    with pytest.raises(ValueError):
        await channel.request(command)
    client.write_gatt_char.assert_not_awaited()


async def test_saved_slot_with_unavailable_buttons_cannot_change_lock(native):
    rest, client, _ = native
    del client.characteristics[api.CHAR_FIRMWARE_REVISION]
    expected = replace(program(), buttons=None)
    with pytest.raises(ValueError, match="Toddler lock"):
        await rest.async_save_program(
            replace(expected, buttons=0x1FF), expected=expected
        )
    assert client.mutations == []


async def test_malformed_occupied_field_fails_whole_table(native):
    rest, client, _ = native
    client.responses[b"EGT01"] = (b"not a date", b"OK")
    with pytest.raises(ProtocolError):
        await rest.async_get_programs()
    assert b"EGL02" not in client.commands


async def test_absent_marker_remains_internal_for_preconditions(native):
    rest, client, _ = native
    client.programs.clear()
    assert not parse_program(1, {"EGL": b"00"}).exists
    assert (await rest.async_get_programs()).programs == ()


async def test_program_connection_timeout_releases_pending_client(native, monkeypatch):
    rest, client, _ = native
    monkeypatch.setattr(api, "PROGRAM_SESSION_TIMEOUT", 0.01)

    async def blocked_connect(*args, **kwargs):
        kwargs["session_callback"](client)
        await asyncio.Event().wait()

    monkeypatch.setattr(api, "establish_connection", blocked_connect)
    with pytest.raises(HatchRestError):
        await rest.async_get_programs()
    client.disconnect.assert_awaited_once()
    client.start_notify.assert_not_awaited()
    assert not rest._lock.locked()


async def test_program_stop_notification_error_still_releases_connection(native):
    rest, client, _ = native
    client.stop_notify.side_effect = BleakError("already disconnected")
    assert (await rest.async_get_programs()).get(1) == program()
    client.disconnect.assert_awaited_once()


async def test_cancelled_program_cleanup_finishes_before_unlocking(native):
    rest, client, _ = native
    entered, release = asyncio.Event(), asyncio.Event()

    async def stop(characteristic):
        entered.set()
        await release.wait()
        await client._stop(characteristic)

    client.stop_notify.side_effect = stop
    task = asyncio.create_task(rest.async_get_programs())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert rest._lock.locked()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client.events[-2:] == ["stop", "disconnect"]
    assert not rest._lock.locked()


async def test_queued_program_timeout_does_not_cancel_current_poll(native, monkeypatch):
    rest, client, connect = native
    entered, release = asyncio.Event(), asyncio.Event()

    async def read(characteristic):
        if characteristic is client.feedback:
            entered.set()
            await release.wait()
        return await client._read(characteristic)

    client.read_gatt_char.side_effect = read
    poll = asyncio.create_task(rest.async_update())
    await entered.wait()
    monkeypatch.setattr(api, "OPERATION_TIMEOUT", 0.01)
    with pytest.raises(HatchRestError):
        await rest.async_get_programs()
    assert not poll.done()
    connect.assert_awaited_once()
    release.set()
    assert (await poll).sound == 3
    client.start_notify.assert_not_awaited()
