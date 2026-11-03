"""Exercise GATT sessions with fake devices and the real transport code."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from _rest_unit import api
from _rest_unit.const import CHAR_FEEDBACK, CHAR_HARDWARE_REVISION, CHAR_TX
from _rest_unit.protocol import HatchRestError, ProtocolError, UnsupportedDeviceError
from bleak import BleakError
from bleak.exc import BleakDBusError, BleakGATTProtocolError, BleakGATTProtocolErrorCode

PACKET = b"T\0\0\0\0C\xff\x20\x40\x80S\x03\0P\0"
NEW_PACKET = b"C\x10\x20\x30\xffS\x07\x80P\0"


class FakeClient:
    def __init__(self):
        self.tx = SimpleNamespace(properties=["write"])
        self.feedback = SimpleNamespace(properties=["read"])
        self.characteristics = {CHAR_TX: self.tx, CHAR_FEEDBACK: self.feedback}
        self.services = SimpleNamespace(get_characteristic=self.characteristics.get)
        self.read_gatt_char = AsyncMock(return_value=PACKET)
        self.write_gatt_char = AsyncMock()
        self.disconnect = AsyncMock()
        self.clear_cache = AsyncMock(return_value=True)
        self.is_connected = True


@pytest.fixture
def session(monkeypatch):
    client = FakeClient()
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    locator = Mock(return_value=device)
    monkeypatch.setattr(api.HatchRestClient, "_get_device", lambda _: locator())
    connect = AsyncMock(return_value=client)
    monkeypatch.setattr(api, "establish_connection", connect)
    monkeypatch.setattr(api, "COMMAND_SETTLE_SECONDS", 0)
    monkeypatch.setattr(api, "RECOVERY_BACKOFF_SECONDS", 0)
    rest = api.HatchRestClient(None, device.address, "Rest")
    return rest, client, connect, locator


async def test_reads_feedback_and_releases_connection(session):
    rest, client, connect, locator = session
    state = await rest.async_update()
    assert state.color == (255, 32, 64)
    assert state.power
    connect.assert_awaited_once()
    locator.assert_called_once()
    client.disconnect.assert_awaited_once()
    client.write_gatt_char.assert_not_awaited()


async def test_each_session_resolves_current_ha_route(session):
    rest, client, connect, locator = session
    await rest.async_update()
    next_device = object()
    locator.return_value = next_device
    await rest.async_update()
    assert connect.await_args.args[1] is next_device
    assert client.disconnect.await_count == 2


async def test_control_uses_fresh_state_and_returns_feedback(session):
    rest, client, _, _ = session
    client.read_gatt_char.side_effect = [PACKET, NEW_PACKET]
    builder = Mock(return_value=[b"SI01", b"SC102030FF"])
    state = await rest.async_control(builder)
    assert builder.call_args.args[0].color == (255, 32, 64)
    assert state.color == (16, 32, 48)
    assert state.sound == 7
    assert client.write_gatt_char.await_args_list[0].args[1] == b"SI01"
    assert client.write_gatt_char.await_args_list[1].args[1] == b"SC102030FF"
    assert client.write_gatt_char.await_args.kwargs == {"response": True}
    client.disconnect.assert_awaited_once()


async def test_without_response_characteristic(session):
    rest, client, _, _ = session
    client.tx.properties = ["write-without-response"]
    await rest.async_control(lambda state: [b"SV00"])
    assert client.write_gatt_char.await_args.kwargs == {"response": False}


@pytest.mark.parametrize("revision,minimum", [(b"1.0", 0), (b"2.0\0", 8)])
async def test_hardware_revision_reads_once_and_attaches_volume_scale(
    session, revision, minimum
):
    rest, client, _, _ = session
    client.characteristics[CHAR_HARDWARE_REVISION] = SimpleNamespace(
        properties=["read"]
    )
    client.read_gatt_char.side_effect = [revision, PACKET, PACKET]
    first = await rest.async_update()
    second = await rest.async_update()
    assert first.hardware_version == revision.decode("ascii").rstrip("\0")
    assert first.volume_min == minimum
    assert second == first
    assert client.read_gatt_char.await_count == 3
    assert client.disconnect.await_count == 2


async def test_builder_receives_hardware_volume_scale(session):
    rest, client, _, _ = session
    client.characteristics[CHAR_HARDWARE_REVISION] = SimpleNamespace(
        properties=["read"]
    )
    client.read_gatt_char.side_effect = [b"2.1", PACKET, PACKET]
    builder = Mock(return_value=[b"SV08"])
    state = await rest.async_control(builder)
    assert builder.call_args.args[0].volume_min == 8
    assert state.volume_min == 8


async def test_failed_hardware_read_is_retryable(session):
    rest, client, _, _ = session
    client.characteristics[CHAR_HARDWARE_REVISION] = SimpleNamespace(
        properties=["read"]
    )
    client.read_gatt_char.side_effect = [BleakError("busy"), b"2.1", PACKET]
    with pytest.raises(HatchRestError):
        await rest.async_update()
    assert (await rest.async_update()).volume_min == 8
    assert client.disconnect.await_count == 2


@pytest.mark.parametrize("missing", [CHAR_TX, CHAR_FEEDBACK])
async def test_other_generations_rejected_and_disconnected(session, missing):
    rest, client, connect, _ = session
    del client.characteristics[missing]
    with pytest.raises(UnsupportedDeviceError):
        await rest.async_update()
    assert client.disconnect.await_count == 2
    assert connect.await_count == 2
    assert connect.await_args.kwargs["use_services_cache"] is False


@pytest.mark.parametrize(
    "error", [BleakError("dropped"), TimeoutError(), OSError("radio")]
)
async def test_read_errors_propagate_and_cleanup(session, error):
    rest, client, _, _ = session
    client.read_gatt_char.side_effect = error
    with pytest.raises(HatchRestError):
        await rest.async_update()
    client.disconnect.assert_awaited_once()


async def test_malformed_packet_never_becomes_success(session):
    rest, client, _, _ = session
    client.read_gatt_char.return_value = b"truncated"
    with pytest.raises(ProtocolError):
        await rest.async_update()
    assert client.disconnect.await_count == 2


async def test_failed_write_never_optimistically_updates(session):
    rest, client, _, _ = session
    client.write_gatt_char.side_effect = BleakError("write failed")
    with pytest.raises(HatchRestError):
        await rest.async_control(lambda state: [b"SVFF"])
    assert client.read_gatt_char.await_count == 1
    client.disconnect.assert_awaited_once()


async def test_control_and_poll_cannot_overlap(session):
    rest, client, connect, _ = session
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_read(*args):
        entered.set()
        await release.wait()
        return PACKET

    client.read_gatt_char.side_effect = blocked_read
    command_task = asyncio.create_task(rest.async_control(lambda state: [b"SVFF"]))
    await entered.wait()
    poll_task = asyncio.create_task(rest.async_update())
    await asyncio.sleep(0)
    assert connect.await_count == 1
    release.set()
    await asyncio.gather(command_task, poll_task)
    assert connect.await_count == 2
    assert client.disconnect.await_count == 2


async def test_cancelled_read_releases_slot(session):
    rest, client, _, _ = session
    entered = asyncio.Event()

    async def read(*args):
        entered.set()
        await asyncio.Event().wait()

    client.read_gatt_char.side_effect = read
    task = asyncio.create_task(rest.async_update())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.disconnect.assert_awaited_once()


async def test_cancelled_connection_releases_pending_client(session, monkeypatch):
    rest, client, _, _ = session
    entered = asyncio.Event()

    async def connect(*args, **kwargs):
        kwargs["session_callback"](client)
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(api, "establish_connection", connect)
    task = asyncio.create_task(rest.async_update())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    client.disconnect.assert_awaited_once()


async def test_closed_client_rejects_queued_work(session):
    rest, client, connect, _ = session
    await rest.async_close()
    await rest.async_close()
    with pytest.raises(HatchRestError, match="closed"):
        await rest.async_update()
    connect.assert_not_awaited()
    client.disconnect.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        BleakGATTProtocolError(BleakGATTProtocolErrorCode.INVALID_HANDLE),
        BleakError(
            "Bluetooth GATT Error address=C5:1F:59:B4:E9:C2 "
            "handle=24 error=1 description=Invalid handle"
        ),
        BleakDBusError("org.bluez.Error.Failed", ["ATT error: 0x01"]),
        BleakError("ESP_GATT_INVALID_HANDLE"),
    ],
)
async def test_invalid_handle_clears_live_cache_and_reconnects(session, error):
    rest, stale, connect, locator = session
    fresh = FakeClient()
    fresh.read_gatt_char.return_value = NEW_PACKET
    fresh_device = object()
    events = []

    async def clear():
        assert rest._client is stale
        events.append("clear")
        return True

    async def disconnect():
        events.append("disconnect")

    stale.clear_cache.side_effect = clear
    stale.disconnect.side_effect = disconnect
    stale.read_gatt_char.side_effect = error
    connect.side_effect = [stale, fresh]
    locator.side_effect = [object(), fresh_device]
    state = await rest.async_update()
    assert state.color == (16, 32, 48)
    assert events == ["clear", "disconnect"]
    assert [call.kwargs["use_services_cache"] for call in connect.await_args_list] == [
        True,
        False,
    ]
    assert connect.await_args.args[1] is fresh_device
    assert fresh.read_gatt_char.await_args.args[0] is fresh.feedback
    fresh.disconnect.assert_awaited_once()
    stale.write_gatt_char.assert_not_awaited()
    fresh.write_gatt_char.assert_not_awaited()


@pytest.mark.parametrize(
    "error",
    [
        BleakError("handle=1 error=5 description=Authentication failed"),
        BleakGATTProtocolError(BleakGATTProtocolErrorCode.INSUFFICIENT_AUTHENTICATION),
        BleakError("org.freedesktop.DBus.Error.UnknownObject"),
    ],
)
async def test_other_gatt_errors_do_not_retry_or_clear_cache(session, error):
    rest, client, connect, _ = session
    client.read_gatt_char.side_effect = error
    with pytest.raises(HatchRestError):
        await rest.async_update()
    connect.assert_awaited_once()
    client.clear_cache.assert_not_awaited()
    client.disconnect.assert_awaited_once()


async def test_repeated_invalid_handle_is_bounded_then_next_poll_can_recover(session):
    rest, first, connect, _ = session
    second, healthy = FakeClient(), FakeClient()
    first.read_gatt_char.side_effect = BleakError("Invalid handle")
    second.read_gatt_char.side_effect = BleakError("Invalid handle")
    connect.side_effect = [first, second, healthy]
    with pytest.raises(HatchRestError, match="Invalid handle"):
        await rest.async_update()
    assert connect.await_count == 2
    first.clear_cache.assert_awaited_once()
    second.clear_cache.assert_awaited_once()
    assert (await rest.async_update()).power
    assert connect.await_args.kwargs["use_services_cache"] is False
    healthy.disconnect.assert_awaited_once()


async def test_recovery_before_commands_uses_fresh_state_once(session):
    rest, stale, connect, _ = session
    fresh = FakeClient()
    stale.read_gatt_char.side_effect = BleakError("Invalid handle")
    fresh.read_gatt_char.return_value = NEW_PACKET
    connect.side_effect = [stale, fresh]
    builder = Mock(return_value=[b"SV80"])
    await rest.async_control(builder)
    builder.assert_called_once()
    assert builder.call_args.args[0].sound == 7
    stale.write_gatt_char.assert_not_awaited()
    fresh.write_gatt_char.assert_awaited_once()


@pytest.mark.parametrize("failure_point", ["write", "readback"])
async def test_invalid_handle_after_write_started_never_replays(session, failure_point):
    rest, client, connect, _ = session
    error = BleakError("Invalid handle")
    if failure_point == "write":
        client.write_gatt_char.side_effect = error
    else:
        client.read_gatt_char.side_effect = [PACKET, error]
    builder = Mock(return_value=[b"SV80", b"SN07"])
    with pytest.raises(HatchRestError):
        await rest.async_control(builder)
    builder.assert_called_once()
    connect.assert_awaited_once()
    assert client.write_gatt_char.await_count == (1 if failure_point == "write" else 2)
    client.clear_cache.assert_awaited_once()
    client.disconnect.assert_awaited_once()
    healthy = FakeClient()
    connect.return_value = healthy
    await rest.async_update()
    assert connect.await_args.kwargs["use_services_cache"] is False
    healthy.write_gatt_char.assert_not_awaited()


@pytest.mark.parametrize("failure", ["missing", "properties", "packet"])
async def test_incompatible_cached_services_get_fresh_verification(session, failure):
    rest, stale, connect, _ = session
    if failure == "missing":
        del stale.characteristics[CHAR_FEEDBACK]
    elif failure == "properties":
        stale.feedback.properties = ["notify"]
    else:
        stale.read_gatt_char.return_value = b"C\x01"
    fresh = FakeClient()
    connect.side_effect = [stale, fresh]
    assert (await rest.async_update()).power
    stale.clear_cache.assert_awaited_once()
    assert connect.await_args.kwargs["use_services_cache"] is False
    fresh.read_gatt_char.assert_awaited_once_with(fresh.feedback)


@pytest.mark.parametrize("failure", ["false", "error", "timeout"])
async def test_cache_clear_failure_still_releases_slot_and_retries(
    session, monkeypatch, failure
):
    rest, stale, connect, _ = session
    stale.read_gatt_char.side_effect = BleakError("Invalid handle")
    if failure == "false":
        stale.clear_cache.return_value = False
    elif failure == "error":
        stale.clear_cache.side_effect = BleakError("proxy went away")
    else:
        monkeypatch.setattr(api, "CACHE_CLEAR_TIMEOUT", 0.01)

        async def blocked():
            await asyncio.Event().wait()

        stale.clear_cache.side_effect = blocked
    fresh = FakeClient()
    connect.side_effect = [stale, fresh]
    assert (await rest.async_update()).power
    stale.disconnect.assert_awaited_once()
    fresh.disconnect.assert_awaited_once()


async def test_off_rest_can_be_read_without_power_or_other_writes(session):
    rest, client, _, _ = session
    client.read_gatt_char.return_value = b"C\xff\x20\x40\x80S\x03\0P\xc0"
    assert not (await rest.async_update()).power
    client.write_gatt_char.assert_not_awaited()
    client.disconnect.assert_awaited_once()


async def test_session_timeout_releases_pending_client_and_unlocks(
    session, monkeypatch
):
    rest, pending, _, _ = session
    monkeypatch.setattr(api, "SESSION_TIMEOUT", 0.01)

    async def blocked_connect(*args, **kwargs):
        kwargs["session_callback"](pending)
        await asyncio.Event().wait()

    monkeypatch.setattr(api, "establish_connection", blocked_connect)
    with pytest.raises(HatchRestError):
        await rest.async_update()
    pending.disconnect.assert_awaited_once()
    fresh = FakeClient()
    monkeypatch.setattr(api, "SESSION_TIMEOUT", 1)
    monkeypatch.setattr(api, "establish_connection", AsyncMock(return_value=fresh))
    assert (await rest.async_update()).power


async def test_queued_operation_deadline_does_not_cancel_current_session(
    session, monkeypatch
):
    rest, client, connect, _ = session
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_read(*args):
        entered.set()
        await release.wait()
        return PACKET

    client.read_gatt_char.side_effect = blocked_read
    current = asyncio.create_task(rest.async_update())
    await entered.wait()
    monkeypatch.setattr(api, "OPERATION_TIMEOUT", 0.01)
    with pytest.raises(HatchRestError):
        await rest.async_update()
    assert not current.done()
    connect.assert_awaited_once()
    release.set()
    assert (await current).power
    monkeypatch.setattr(api, "OPERATION_TIMEOUT", 1)
    assert (await rest.async_update()).power


async def test_live_disconnect_failure_blocks_new_client_until_cleanup(session):
    rest, client, connect, _ = session
    client.disconnect.side_effect = BleakError("proxy busy")
    await rest.async_update()
    with pytest.raises(HatchRestError, match="could not be released"):
        await rest.async_update()
    connect.assert_awaited_once()
    assert rest._client is client
    client.disconnect.side_effect = None
    fresh = FakeClient()
    connect.return_value = fresh
    assert (await rest.async_update()).power
    assert client.disconnect.await_count == 3
    fresh.disconnect.assert_awaited_once()


async def test_dead_proxy_disconnect_error_does_not_block_next_session(session):
    rest, client, connect, _ = session

    async def dead_disconnect():
        client.is_connected = False
        raise BleakError("proxy API disconnected")

    client.disconnect.side_effect = dead_disconnect
    await rest.async_update()
    fresh = FakeClient()
    connect.return_value = fresh
    assert (await rest.async_update()).power
    client.disconnect.assert_awaited_once()
    assert connect.await_count == 2


async def test_cancelled_cache_clear_releases_connection_and_next_poll_is_fresh(
    session,
):
    rest, stale, connect, _ = session
    entered = asyncio.Event()
    stale.read_gatt_char.side_effect = BleakError("Invalid handle")

    async def clear():
        entered.set()
        await asyncio.Event().wait()

    stale.clear_cache.side_effect = clear
    task = asyncio.create_task(rest.async_update())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stale.disconnect.assert_awaited_once()
    fresh = FakeClient()
    connect.return_value = fresh
    await rest.async_update()
    assert connect.await_args.kwargs["use_services_cache"] is False


async def test_confirmed_readback_is_published_before_slow_disconnect(session):
    """Proxy teardown may be slow, but confirmed state can reach HA earlier."""
    rest, client, connect, _ = session
    entered, release = asyncio.Event(), asyncio.Event()
    client.read_gatt_char.side_effect = [PACKET, NEW_PACKET, NEW_PACKET]
    received = Mock()

    async def disconnect():
        received.assert_called_once()
        entered.set()
        await release.wait()

    client.disconnect.side_effect = disconnect
    action = asyncio.create_task(
        rest.async_control(lambda state: [b"SN07"], on_state=received)
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert received.call_args.args[0].sound == 7
        assert received.call_args.args[0].color == (16, 32, 48)
        assert not action.done()
        assert rest._lock.locked()
        poll = asyncio.create_task(rest.async_update())
        await asyncio.sleep(0)
        connect.assert_awaited_once()
    finally:
        release.set()
        await action
    assert (await poll).sound == 7
    assert connect.await_count == 2
    received.assert_called_once()


@pytest.mark.parametrize("failure_point", ["initial_read", "write", "readback"])
async def test_failed_operation_never_publishes_requested_state(session, failure_point):
    rest, client, _, _ = session
    error = BleakError("radio disconnected")
    if failure_point == "initial_read":
        client.read_gatt_char.side_effect = error
    elif failure_point == "write":
        client.write_gatt_char.side_effect = error
    else:
        client.read_gatt_char.side_effect = [PACKET, error]
    received = Mock()
    with pytest.raises(HatchRestError):
        await rest.async_control(lambda state: [b"SN07"], on_state=received)
    received.assert_not_called()
    client.disconnect.assert_awaited_once()


async def test_recovered_read_publishes_only_fresh_final_feedback(session):
    rest, stale, connect, _ = session
    stale.read_gatt_char.side_effect = BleakError("Invalid handle")
    fresh = FakeClient()
    fresh.read_gatt_char.side_effect = [PACKET, NEW_PACKET]
    connect.side_effect = [stale, fresh]
    received = Mock()
    await rest.async_control(lambda state: [b"SN07"], on_state=received)
    received.assert_called_once()
    assert received.call_args.args[0].sound == 7
    assert received.call_args.args[0].color == (16, 32, 48)


async def test_cancelled_cleanup_does_not_replay_or_revoke_confirmed_feedback(session):
    rest, client, connect, _ = session
    entered, release = asyncio.Event(), asyncio.Event()
    client.read_gatt_char.side_effect = [PACKET, NEW_PACKET]

    async def disconnect():
        entered.set()
        await release.wait()

    client.disconnect.side_effect = disconnect
    received = Mock()
    action = asyncio.create_task(
        rest.async_control(lambda state: [b"SN07"], on_state=received)
    )
    await asyncio.wait_for(entered.wait(), 1)
    action.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await action
    received.assert_called_once()
    connect.assert_awaited_once()
    client.write_gatt_char.assert_awaited_once()
    client.disconnect.assert_awaited_once()
    assert rest._client is None
