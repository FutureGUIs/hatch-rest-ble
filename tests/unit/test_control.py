"""Native wake and power behavior without overriding firmware-restored outputs."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from _rest_unit import api
from _rest_unit.const import CHAR_FEEDBACK, CHAR_TX
from _rest_unit.control import (
    light_on_commands,
    power_commands,
    sound_on_commands,
)
from _rest_unit.protocol import RestState

BASE = RestState((20, 40, 60), 128, 5, 100, True, 0)


@pytest.mark.parametrize("power", [False, True])
def test_light_commands_use_native_wake_without_sound_or_volume_writes(power):
    state = replace(BASE, power=power)
    commands = light_on_commands(state, (1, 2, 3), 50)
    assert commands == ([b"SC01020332"] if power else [b"SI01", b"SC01020332"])


def test_zero_brightness_does_not_wake_or_silence_audio():
    assert light_on_commands(replace(BASE, power=False), BASE.color, 0) == [
        b"SC14283C00"
    ]


@pytest.mark.parametrize("power", [False, True])
def test_sound_commands_use_native_wake_without_light_or_volume_writes(power):
    commands = sound_on_commands(replace(BASE, power=power), 7)
    assert commands == ([b"SN07"] if power else [b"SI01", b"SN07"])


def test_silence_and_repeated_play_do_not_wake_the_device():
    assert sound_on_commands(replace(BASE, power=False), 0) == [b"SN00"]
    assert sound_on_commands(BASE, BASE.sound) == []
    assert sound_on_commands(BASE, BASE.sound, force=True) == [b"SN05"]


class WakePresetFirmware:
    """A Rest whose SI01 resumes an unrelated preset and off reads report zeros."""

    def __init__(self, initial):
        self.state = initial
        self.commands = []
        self.wake_state = RestState((10, 9, 8), 255, 7, 180, True, 0)
        self.tx = SimpleNamespace(properties=["write"])
        self.feedback = SimpleNamespace(properties=["read"])
        chars = {CHAR_TX: self.tx, CHAR_FEEDBACK: self.feedback}
        self.services = SimpleNamespace(get_characteristic=chars.get)
        self.disconnect = AsyncMock()
        self.clear_cache = AsyncMock(return_value=True)
        self.is_connected = True

    async def read_gatt_char(self, characteristic):
        state = self.state
        if not state.power:
            state = replace(state, brightness=0, sound=0)
        return (
            b"C"
            + bytes((*state.color, state.brightness))
            + b"S"
            + bytes((state.sound, state.volume))
            + b"P"
            + bytes((0 if state.power else 0xC0,))
        )

    async def write_gatt_char(self, characteristic, command, *, response):
        assert characteristic is self.tx
        self.commands.append(command)
        if command == b"SI01":
            self.state = replace(self.wake_state, power=True)
        elif command == b"SI00":
            self.wake_state = self.state
            self.state = replace(self.state, power=False)
        elif command.startswith(b"SC"):
            raw = bytes.fromhex(command[2:].decode())
            self.state = replace(self.state, color=tuple(raw[:3]), brightness=raw[3])
        elif command.startswith(b"SN"):
            self.state = replace(self.state, sound=int(command[2:], 16))
        elif command.startswith(b"SV"):
            self.state = replace(self.state, volume=int(command[2:], 16))
        else:
            pytest.fail(f"Unexpected command {command!r}")


@pytest.fixture
def firmware_session(monkeypatch):
    firmware = WakePresetFirmware(replace(BASE, power=False))
    device = SimpleNamespace(address="AA:BB:CC:DD:EE:FF")
    monkeypatch.setattr(api.HatchRestClient, "_get_device", lambda _: device)
    monkeypatch.setattr(api, "establish_connection", AsyncMock(return_value=firmware))
    monkeypatch.setattr(api, "COMMAND_SETTLE_SECONDS", 0)
    rest = api.HatchRestClient(None, device.address, "Rest")
    return rest, firmware


async def test_real_transport_light_wake_preserves_firmware_sound_and_volume(
    firmware_session,
):
    rest, firmware = firmware_session
    state = await rest.async_control(
        lambda fresh: light_on_commands(fresh, (1, 2, 3), 64)
    )
    assert state.power
    assert state.brightness == 64
    assert state.color == (1, 2, 3)
    assert state.sound == firmware.wake_state.sound
    assert state.volume == firmware.wake_state.volume
    assert firmware.commands == [b"SI01", b"SC01020340"]


async def test_real_transport_sound_wake_preserves_firmware_lamp_and_volume(
    firmware_session,
):
    rest, firmware = firmware_session
    state = await rest.async_control(lambda fresh: sound_on_commands(fresh, 250))
    assert state.power
    assert state.sound == 250
    assert state.brightness == firmware.wake_state.brightness
    assert state.color == firmware.wake_state.color
    assert state.volume == firmware.wake_state.volume
    assert firmware.commands == [b"SI01", b"SNFA"]


@pytest.mark.parametrize("brightness,sound", [(128, 5), (128, 0), (0, 5), (0, 0)])
async def test_master_power_leaves_output_restoration_to_firmware(
    firmware_session, brightness, sound
):
    rest, firmware = firmware_session
    original = replace(BASE, brightness=brightness, sound=sound, color=(254, 254, 254))
    firmware.state = original
    off = await rest.async_control(lambda state: power_commands(state, False))
    assert not off.power
    assert off.brightness == off.sound == 0
    restored = await rest.async_control(lambda state: power_commands(state, True))
    assert firmware.commands == [b"SI00", b"SI01"]
    assert restored.color == original.color
    assert restored.brightness == original.brightness
    assert restored.sound == original.sound
    assert restored.volume == original.volume
    assert restored.power


async def test_repeated_power_requests_leave_firmware_preset_untouched(
    firmware_session,
):
    rest, firmware = firmware_session
    expected = firmware.wake_state
    await rest.async_control(lambda fresh: power_commands(fresh, False))
    restored = await rest.async_control(lambda fresh: power_commands(fresh, True))
    repeated = await rest.async_control(lambda fresh: power_commands(fresh, True))
    assert restored == repeated == expected
    assert firmware.commands == [b"SI01"]
    await rest.async_control(lambda fresh: power_commands(fresh, False))
    await rest.async_control(lambda fresh: power_commands(fresh, False))
    assert firmware.commands == [b"SI01", b"SI00"]


async def test_output_actions_preserve_an_already_active_counterpart(firmware_session):
    rest, firmware = firmware_session
    firmware.state = BASE
    state = await rest.async_control(
        lambda fresh: light_on_commands(fresh, (3, 4, 5), 90)
    )
    assert state.sound == BASE.sound
    assert state.volume == BASE.volume
    state = await rest.async_control(lambda fresh: sound_on_commands(fresh, 9))
    assert state.color == (3, 4, 5)
    assert state.brightness == 90


@pytest.mark.parametrize("output", ["light", "sound"])
async def test_dark_silent_standby_starts_only_requested_output(
    firmware_session, output
):
    rest, firmware = firmware_session
    firmware.state = replace(BASE, power=True, brightness=0, sound=0)
    if output == "light":
        state = await rest.async_control(
            lambda fresh: light_on_commands(fresh, (1, 2, 3), 64)
        )
        assert state.brightness == 64
        assert state.sound == 0
        assert firmware.commands == [b"SC01020340"]
    else:
        state = await rest.async_control(lambda fresh: sound_on_commands(fresh, 7))
        assert state.brightness == 0
        assert state.sound == 7
        assert firmware.commands == [b"SN07"]
