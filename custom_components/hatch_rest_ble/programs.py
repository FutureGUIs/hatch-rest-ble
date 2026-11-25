"""Original Rest native programs and their classic ASCII command format."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from datetime import datetime, time

from .protocol import HatchRestError, ProtocolError

PROGRAM_SLOTS = 10
FLAG_EXISTS = 0x80
FLAG_ENABLED = 0x40
TODDLER_LOCK_MASK = 0x01FF
MIN_DURATION = 60
MAX_DURATION = 17 * 3600 + 59 * 60


class ProgramConflictError(HatchRestError):
    """A program changed on the device while it was being edited."""


class ProgramVerificationError(HatchRestError):
    """The device did not confirm the expected program after a write."""


@dataclass(frozen=True)
class RestProgram:
    """A native slot, with unconverted wire values and a local wall-clock start."""

    slot: int
    name: str
    start: datetime
    duration: int
    days: int
    color: tuple[int, int, int]
    brightness: int
    sound: int
    volume: int
    flags: int
    buttons: int | None
    power: int = 255
    presets: int = 0

    @property
    def exists(self) -> bool:
        """Whether this slot holds a native program."""
        return bool(self.flags & FLAG_EXISTS)

    @property
    def enabled(self) -> bool:
        """Whether the device's native program is enabled."""
        return bool(self.flags & FLAG_ENABLED)

    @property
    def toddler_lock(self) -> bool:
        """Whether all eight named physical-button restrictions are set."""
        return self.buttons is not None and self.buttons & 0xFF == 0xFF


@dataclass(frozen=True)
class ProgramSnapshot:
    """A complete table read containing occupied slots and device metadata."""

    programs: tuple[RestProgram, ...]
    device_time: datetime | None = None
    firmware_version: str | None = None
    volume_min: int = 0

    @property
    def supports_toddler_lock(self) -> bool:
        """Apply the app's dotted-version-to-integer firmware gate."""
        return supports_toddler_lock(self.firmware_version)

    def get(self, slot: int) -> RestProgram | None:
        """Return an occupied slot, or None when it is absent."""
        return next(
            (program for program in self.programs if program.slot == slot), None
        )


def supports_toddler_lock(firmware_version: str | None) -> bool:
    """The legacy app enables per-program buttons at firmware 10431 or newer."""
    if (
        not isinstance(firmware_version, str)
        or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*", firmware_version)
        or len(firmware_version) > 32
    ):
        return False
    return int(firmware_version.replace(".", "")) >= 10431


def _integer(value: int, minimum: int, maximum: int, field: str) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer from {minimum} to {maximum}")


def _slot(slot: int) -> None:
    _integer(slot, 1, PROGRAM_SLOTS, "Program slot")


def _wall_time(value: datetime) -> None:
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise ValueError("Program time must be a naive local datetime")
    if value.microsecond:
        raise ValueError("Program time must use whole seconds")


def format_clock(value: datetime) -> str:
    """Format the app's local yyyyMMddHHmmss without issuing a clock write."""
    _wall_time(value)
    # strftime year padding is platform-dependent for years below 1000.
    return (
        f"{value.year:04d}{value.month:02d}{value.day:02d}"
        f"{value.hour:02d}{value.minute:02d}{value.second:02d}"
    )


def parse_clock(data: bytes) -> datetime:
    """Parse an exact GT/EGT local wall-clock reply, without timezone guessing."""
    if not isinstance(data, bytes) or not re.fullmatch(rb"[0-9]{14}", data):
        raise ProtocolError("Expected a yyyyMMddHHmmss device time")
    try:
        text = data.decode("ascii")
        return datetime(
            int(text[:4]),
            int(text[4:6]),
            int(text[6:8]),
            int(text[8:10]),
            int(text[10:12]),
            int(text[12:14]),
        )
    except ValueError as error:
        raise ProtocolError("Device time contains an invalid date or time") from error


def _hex(responses: dict[str, bytes], key: str, digits: int) -> int:
    data = responses.get(key)
    if not isinstance(data, bytes) or not re.fullmatch(
        rb"[0-9A-Fa-f]{1," + str(digits).encode("ascii") + rb"}", data
    ):
        raise ProtocolError(f"Missing or malformed program field {key}")
    return int(data, 16)


def parse_program(slot: int, responses: dict[str, bytes]) -> RestProgram:
    """Decode classic field replies; an occupied slot must be fully reported."""
    _slot(slot)
    flags = _hex(responses, "EGL", 2)
    if not flags & FLAG_EXISTS:
        # A missing slot has no reported controls. Its values are never published
        # as an existing program or used to fill missing occupied-record fields.
        return RestProgram(
            slot, "", datetime.min, 0, 0, (0, 0, 0), 0, 0, 0, flags, None
        )

    color_reply = responses.get("EGC")
    if not isinstance(color_reply, bytes) or not re.fullmatch(
        rb"[0-9A-Fa-f]{8}", color_reply
    ):
        raise ProtocolError("Missing or malformed program field EGC")
    rgba = bytes.fromhex(color_reply.decode("ascii"))
    days = _hex(responses, "EGW", 2)
    if days > 0x7F:
        raise ProtocolError("Program weekday mask contains an unknown bit")
    name = responses.get("EGX")
    if not isinstance(name, bytes):
        raise ProtocolError("Missing program field EGX")
    try:
        decoded_name = name.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProtocolError("Program name is not valid UTF-8") from error
    if any(ord(char) < 32 or ord(char) == 127 for char in decoded_name):
        raise ProtocolError("Program name contains control characters")
    if "EGT" not in responses:
        raise ProtocolError("Missing program field EGT")
    return RestProgram(
        slot=slot,
        name=decoded_name,
        start=parse_clock(responses["EGT"]),
        duration=_hex(responses, "EGD", 4),
        days=days,
        color=(rgba[0], rgba[1], rgba[2]),
        brightness=rgba[3],
        sound=_hex(responses, "EGN", 2),
        volume=_hex(responses, "EGV", 2),
        flags=flags,
        # Buttons.f parses the entire reply as hex. ESM's trailing duration
        # argument does not establish a corresponding suffix in EGM replies.
        buttons=_hex(responses, "EGM", 8) if "EGM" in responses else None,
        power=_hex(responses, "EGI", 2),
        presets=_hex(responses, "EGP", 8),
    )


def validate_program(program: RestProgram) -> None:
    """Check safe full-write bounds, independently of readable legacy records."""
    _slot(program.slot)
    _wall_time(program.start)
    _integer(program.duration, MIN_DURATION, MAX_DURATION, "Program duration")
    _integer(program.days, 0, 0x7F, "Program repeat days")
    if (
        not isinstance(program.name, str)
        or not 1 <= len(program.name) <= 16
        or not program.name.strip()
        or any(not 32 <= ord(char) <= 126 for char in program.name)
    ):
        raise ValueError("Program name must contain 1 to 16 printable ASCII characters")
    if program.name in {"OK", "E01", "E02", "E03", "E04", "E05", "E06", "Unknown"}:
        raise ValueError("Program name is reserved by the device reply protocol")
    if not isinstance(program.color, tuple) or len(program.color) != 3:
        raise ValueError("Program color must be an RGB tuple")
    for channel in program.color:
        _integer(channel, 0, 255, "RGB channel")
    for field in ("brightness", "sound", "volume", "flags", "power"):
        _integer(getattr(program, field), 0, 255, f"Program {field}")
    _integer(program.presets, 0, 0xFFFFFFFF, "Program presets")
    if program.buttons is not None:
        _integer(program.buttons, 0, 0xFFFF, "Program buttons")
    if not program.exists:
        raise ValueError("A full program write must set its exists flag")


def program_commands(
    program: RestProgram, *, supports_toddler_lock: bool
) -> list[bytes]:
    """Encode the app's begin/fields/finish sequence; preserve opaque settings."""
    validate_program(program)
    commands = [
        f"ESB{program.slot:02X}",
        "EST" + format_clock(program.start),
        f"ESD{program.duration:04X}",
        f"ESW{program.days:02X}",
        f"ESI{program.power:02X}",
        "ESC"
        + "".join(f"{value:02X}" for value in (*program.color, program.brightness)),
        f"ESN{program.sound:02X}",
        f"ESV{program.volume:02X}",
        "ESX" + program.name,
    ]
    if supports_toddler_lock and program.buttons is not None:
        commands.append(f"ESM{program.buttons:04X}0000")
    # EGP is read-only in the inspected app's writer; do not invent an ESP field.
    commands.extend((f"ESL{program.flags:02X}", "ESF"))
    return [command.encode("ascii") for command in commands]


def enabled_commands(program: RestProgram, enabled: bool) -> list[bytes]:
    """Change only native enable state, preserving every other classic flag."""
    _slot(program.slot)
    _integer(program.flags, 0, 255, "Program flags")
    if type(enabled) is not bool:
        raise ValueError("Program enabled must be a boolean")
    if not program.exists:
        raise ValueError("Cannot enable or disable an absent program")
    flags = program.flags | FLAG_ENABLED if enabled else program.flags & ~FLAG_ENABLED
    return [f"ESB{program.slot:02X}".encode(), f"ESL{flags:02X}".encode(), b"ESF"]


def delete_commands(slot: int) -> list[bytes]:
    """Clear one native slot with the app's flags-and-finish sequence."""
    _slot(slot)
    return [f"ESB{slot:02X}".encode(), b"ESL00", b"ESF"]


# Bit for each weekday in the native repeat mask, Monday first.
WEEKDAY_BITS = (
    ("monday", "Monday", 0x02),
    ("tuesday", "Tuesday", 0x04),
    ("wednesday", "Wednesday", 0x08),
    ("thursday", "Thursday", 0x10),
    ("friday", "Friday", 0x20),
    ("saturday", "Saturday", 0x40),
    ("sunday", "Sunday", 0x01),
)
DEFAULT_START = time(20)
DEFAULT_DURATION = 3600


def new_program(
    slot: int,
    *,
    today: datetime,
    color: tuple[int, int, int],
    brightness: int,
    sound: int,
    volume: int,
    supports_lock: bool,
    name: str | None = None,
) -> RestProgram:
    """The defaults used whenever HA adds a program: 8 PM, 1 hour, every day."""
    return RestProgram(
        slot=slot,
        name=name if name is not None else f"Program {slot}",
        start=datetime.combine(today.date(), DEFAULT_START),
        duration=DEFAULT_DURATION,
        days=0x7F,
        color=color,
        brightness=brightness,
        sound=sound,
        volume=volume,
        flags=FLAG_EXISTS | FLAG_ENABLED,
        buttons=0 if supports_lock else None,
    )


def brightness_percent(raw: int) -> int:
    """Native 0-255 program brightness as the app's whole percentage."""
    return math.floor(raw * 100 / 255 + 0.5)


def brightness_raw(percent: int, current: int) -> int:
    """Keep the exact native value when the displayed percentage is unchanged."""
    if percent == brightness_percent(current):
        return current
    return math.floor(percent * 255 / 100 + 0.5)


def volume_percent(raw: int, minimum: int) -> int:
    """Native volume scaled above the hardware revision's minimum."""
    return max(0, math.floor((raw - minimum) * 100 / (255 - minimum) + 0.5))


def volume_raw(percent: int, current: int, minimum: int) -> int:
    """Keep the exact native value when the displayed percentage is unchanged."""
    if percent == volume_percent(current, minimum):
        return current
    return math.floor(percent * (255 - minimum) / 100 + 0.5) + minimum


def with_toddler_lock(program: RestProgram, locked: bool) -> RestProgram:
    """Set or clear all eight named button restrictions, keeping other bits."""
    buttons = program.buttons or 0
    buttons = buttons | TODDLER_LOCK_MASK if locked else buttons & ~TODDLER_LOCK_MASK
    return replace(program, buttons=buttons)


def with_enabled(program: RestProgram, enabled: bool) -> RestProgram:
    """Set the native enable flag, keeping every other flag bit."""
    flags = program.flags | FLAG_ENABLED if enabled else program.flags & ~FLAG_ENABLED
    return replace(program, flags=flags)


def program_device_key(address: str, slot: int) -> str:
    """Device-registry identifier for one program slot of one Rest."""
    return f"{address}:program:{slot}"


def program_slot_from_key(address: str, key: str) -> int | None:
    """Slot of a program device identifier or entity unique ID, if it is one."""
    prefix = f"{address}:program:"
    if not key.startswith(prefix):
        return None
    head = key[len(prefix) :].split(":", 1)[0]
    if not head.isdigit() or not 1 <= int(head) <= PROGRAM_SLOTS:
        return None
    return int(head)
