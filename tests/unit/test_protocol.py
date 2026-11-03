"""Wire-format regression tests backed by the Android app's tagged parser."""

import pytest
from _rest_unit.protocol import (
    ProtocolError,
    color_command,
    parse_feedback,
    power_command,
    sound_command,
    volume_command,
)


@pytest.mark.parametrize(
    "flags,on",
    [(0, True), (0x40, True), (0x80, True), (0xC0, False), (1, True), (0xFF, False)],
)
def test_power_flags(flags, on):
    state = parse_feedback(b"T\0\0\0\0C\xff\x20\x40\x80S\x03\0P" + bytes([flags]))
    assert state.power is on
    assert state.raw_flags == flags
    assert state.volume == 0
    assert state.color == (255, 32, 64)
    assert state.brightness == 128


@pytest.mark.parametrize(
    "packet",
    [
        b"C\x01\x02\x03\0S\0\xffP\0",
        b"P\0S\0\xffC\x01\x02\x03\0",
        b"e\x80E\x01\x02\x03\x04\x05S\0\xffP\0T\0\0\0\0C\x01\x02\x03\0",
    ],
)
def test_tag_order_and_optional_fields(packet):
    state = parse_feedback(packet)
    assert state.color == (1, 2, 3)
    assert state.sound == 0
    assert state.brightness == 0
    assert state.volume == 255


def test_payload_bytes_are_not_field_markers():
    state = parse_feedback(b"CTCSP" + b"S\xf0\0P\0")
    assert state.color == (84, 67, 83)
    assert state.brightness == 80
    assert state.sound == 240


@pytest.mark.parametrize(
    "packet", [b"", b"P", b"C\x01", b"S\x03", b"T\0", b"C\0\0\0\0P\0", b"E\0\0"]
)
def test_reject_incomplete_feedback(packet):
    with pytest.raises(ProtocolError):
        parse_feedback(packet)


def test_commands_are_ascii_hex_and_boundaries():
    assert power_command(True) == b"SI01"
    assert power_command(False) == b"SI00"
    assert color_command((255, 0, 128), 0) == b"SCFF008000"
    assert color_command((254, 254, 254), 255) == b"SCFEFEFEFF"
    assert sound_command(14) == b"SN0E"
    assert volume_command(0) == b"SV00"
    assert volume_command(255) == b"SVFF"


@pytest.mark.parametrize("value", [-1, 256, 0.5, True, "1"])
def test_reject_out_of_range_bytes(value):
    with pytest.raises(ValueError):
        volume_command(value)
    with pytest.raises(ValueError):
        color_command((0, 0, 0), value)
