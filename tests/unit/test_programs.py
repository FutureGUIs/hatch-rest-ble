"""Native schedules: evidence-backed field encoding and unsafe reply rejection."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest
from _rest_unit.programs import (
    MAX_DURATION,
    ProgramConflictError,
    ProgramSnapshot,
    ProgramVerificationError,
    RestProgram,
    delete_commands,
    enabled_commands,
    format_clock,
    parse_clock,
    parse_program,
    program_commands,
    supports_toddler_lock,
    validate_program,
)
from _rest_unit.protocol import HatchRestError, ProtocolError

# Classic replies corresponding to Program.java's EGC/D/I/M/N/P/T/V/X/W reads.
REPLIES = {
    "EGL": b"E5",  # Exists, enabled, and opaque lower flags must all survive.
    "EGC": b"12345680",
    "EGD": b"0E10",
    "EGI": b"FF",
    "EGM": b"01FF",
    "EGN": b"03",
    "EGP": b"20",
    "EGT": b"20261015233030",
    "EGV": b"7F",
    "EGX": b"Bedtime",
    "EGW": b"3E",  # Monday through Friday; Sunday is bit zero.
}
BASE = RestProgram(
    slot=2,
    name="Bedtime",
    start=datetime(2026, 10, 15, 23, 30, 30),
    duration=3600,
    days=0x3E,
    color=(0x12, 0x34, 0x56),
    brightness=128,
    sound=3,
    volume=127,
    flags=0xE5,
    buttons=0x1FF,
    power=255,
    presets=0x20,
)


def test_classic_read_preserves_all_reported_settings():
    assert parse_program(2, REPLIES) == BASE
    assert BASE.exists and BASE.enabled and BASE.toddler_lock
    assert BASE.start + timedelta(seconds=BASE.duration) == datetime(
        2026, 10, 16, 0, 30, 30
    )
    with pytest.raises(FrozenInstanceError):
        BASE.name = "Changed"


def test_exact_app_write_sequence_and_classic_roundtrip():
    commands = program_commands(BASE, supports_toddler_lock=True)
    assert commands == [
        b"ESB02",
        b"EST20261015233030",
        b"ESD0E10",
        b"ESW3E",
        b"ESIFF",
        b"ESC12345680",
        b"ESN03",
        b"ESV7F",
        b"ESXBedtime",
        b"ESM01FF0000",
        b"ESLE5",
        b"ESF",
    ]
    # EGP has no writer in the original app and must remain device-reported.
    replies = {"EGP": REPLIES["EGP"]}
    for command in commands[1:-1]:
        key = command[:3].decode().replace("ES", "EG", 1)
        replies[key] = command[3:7] if key == "EGM" else command[3:]
    assert parse_program(2, replies) == BASE


def test_enable_disable_and_delete_preserve_opaque_flags():
    assert enabled_commands(BASE, False) == [b"ESB02", b"ESLA5", b"ESF"]
    assert enabled_commands(replace(BASE, flags=0xA5), True) == [
        b"ESB02",
        b"ESLE5",
        b"ESF",
    ]
    assert delete_commands(10) == [b"ESB0A", b"ESL00", b"ESF"]
    # Toggle is safe even for a readable program that cannot be fully rewritten.
    assert enabled_commands(replace(BASE, name="Caf\u00e9", duration=30), False)


def test_absent_slot_requires_no_invented_occupied_fields():
    empty = parse_program(1, {"EGL": b"20"})
    assert empty.slot == 1 and empty.flags == 0x20 and not empty.exists
    with pytest.raises(ValueError, match="absent"):
        enabled_commands(empty, True)


def test_firmware_gate_follows_dotted_version_rule_conservatively():
    for version in ("10431", "1.0.4.31", "1.0.4.32", "2.0.0.00"):
        assert supports_toddler_lock(version)
    for version in (None, "", "1.0.4.30", "10430", "v10431", "10431junk", "1..0431"):
        assert not supports_toddler_lock(version)
    snapshot = ProgramSnapshot((BASE,), firmware_version="1.0.4.31")
    assert snapshot.supports_toddler_lock
    assert snapshot.get(2) == BASE and snapshot.get(1) is None


def test_unsupported_buttons_are_retained_but_not_written():
    program = parse_program(
        2, {key: val for key, val in REPLIES.items() if key != "EGM"}
    )
    assert program.buttons is None and not program.toddler_lock
    assert not any(
        cmd.startswith(b"ESM")
        for cmd in program_commands(BASE, supports_toddler_lock=False)
    )
    assert BASE.buttons == 0x1FF


def test_button_reply_is_an_integer_not_write_mask_plus_duration():
    padded = parse_program(2, REPLIES | {"EGM": b"000001FF"})
    assert padded.buttons == 0x1FF and padded.toddler_lock
    # No APK evidence allows discarding high bits as if EGM echoed ESM's
    # trailing duration. Preserve unknown settings and refuse a full rewrite.
    unknown = parse_program(2, REPLIES | {"EGM": b"01FF0000"})
    assert unknown.buttons == 0x01FF0000 and not unknown.toddler_lock
    with pytest.raises(ValueError, match="buttons"):
        program_commands(unknown, supports_toddler_lock=True)
    assert enabled_commands(unknown, False) == [b"ESB02", b"ESLA5", b"ESF"]


@pytest.mark.parametrize("key", [key for key in REPLIES if key != "EGM"])
def test_missing_occupied_field_rejected_except_optional_buttons(key):
    with pytest.raises(ProtocolError):
        parse_program(2, {k: val for k, val in REPLIES.items() if k != key})


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("EGL", b"100"),
        ("EGL", b" C0"),
        ("EGL", b"OK"),
        ("EGC", b"1234567"),
        ("EGC", b"1234567Z"),
        ("EGD", b"10000"),
        ("EGV", b"-1"),
        ("EGN", b"01\n"),
        ("EGW", b"80"),
        ("EGT", b"20260230000000"),
        ("EGT", b"20261015246000"),
        ("EGX", b"name\nESF"),
        ("EGX", b"\xff"),
        ("EGM", b"1" * 9),
    ],
)
def test_malformed_or_truncated_fields_fail_before_commands(key, value):
    with pytest.raises(ProtocolError):
        parse_program(2, REPLIES | {key: value})


def test_readable_unicode_and_unusual_native_values_are_not_normalized():
    program = parse_program(
        2,
        REPLIES
        | {
            "EGX": "Caf\u00e9".encode(),
            "EGD": b"FFFF",
            "EGM": b"12345678",
            "EGW": b"00",
        },
    )
    assert program.name == "Caf\u00e9" and program.duration == 65535
    assert program.buttons == 0x12345678 and program.days == 0
    with pytest.raises(ValueError):
        validate_program(program)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("slot", True),
        ("slot", 0),
        ("slot", 11),
        ("duration", True),
        ("duration", 59),
        ("duration", MAX_DURATION + 1),
        ("days", 128),
        ("days", False),
        ("brightness", -1),
        ("sound", 256),
        ("volume", True),
        ("flags", 256),
        ("flags", 0x40),
        ("power", -1),
        ("buttons", True),
        ("buttons", 65536),
        ("presets", False),
        ("color", (0, 1, True)),
        ("color", [1, 2, 3]),
        ("name", ""),
        ("name", " "),
        ("name", "x" * 17),
        ("name", "Hello\nESF"),
        ("name", "Cafe\u00e9"),
        ("name", "OK"),
        ("name", "E01"),
        ("name", "Unknown"),
        ("start", datetime(2026, 10, 15, tzinfo=UTC)),
        ("start", datetime(2026, 10, 15, microsecond=1)),
        ("start", "20261015000000"),
    ],
)
def test_invalid_full_writes_are_rejected(field, value):
    with pytest.raises(ValueError):
        program_commands(replace(BASE, **{field: value}), supports_toddler_lock=True)


@pytest.mark.parametrize("duration", [60, 3630, MAX_DURATION])
def test_valid_duration_bounds_preserve_native_subminute_seconds(duration):
    validate_program(replace(BASE, duration=duration))


def test_zero_days_remains_representable_without_inventing_one_shot_semantics():
    assert b"ESW00" in program_commands(
        replace(BASE, days=0), supports_toddler_lock=True
    )


def test_exact_clock_roundtrip_and_invalid_inputs():
    value = datetime(2024, 2, 29, 0, 1, 2)
    assert format_clock(value) == "20240229000102"
    assert parse_clock(format_clock(value).encode()) == value
    for data in (
        b"20240230000102",
        b"20240229000102N",
        b"2024022900010",
        b"+0240229000102",
    ):
        with pytest.raises(ProtocolError):
            parse_clock(data)
    assert issubclass(ProgramConflictError, HatchRestError)
    assert issubclass(ProgramVerificationError, HatchRestError)


@pytest.mark.parametrize("slot", [0, 11, True, "1"])
def test_invalid_slot_never_encodes_a_write(slot):
    with pytest.raises(ValueError):
        delete_commands(slot)
