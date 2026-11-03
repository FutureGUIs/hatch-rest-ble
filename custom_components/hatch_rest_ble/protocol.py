"""First-generation Rest wire format, independent of Home Assistant."""

from dataclasses import dataclass


class HatchRestError(Exception):
    """A device operation failed."""


class UnsupportedDeviceError(HatchRestError):
    """The device does not expose the first-generation Rest protocol."""


class ProtocolError(HatchRestError):
    """A feedback packet has an unexpected layout."""


@dataclass(frozen=True)
class RestState:
    """One complete device-reported state; brightness and volume are bytes."""

    color: tuple[int, int, int]
    brightness: int
    sound: int
    volume: int
    power: bool
    raw_flags: int
    hardware_version: str | None = None
    volume_min: int = 0
    firmware_version: str | None = None


def parse_feedback(data: bytes | bytearray) -> RestState:
    """Decode tagged legacy feedback without converting unknown sound IDs."""
    # The Android app iterates tags, rather than assuming fixed field offsets.
    widths = {
        ord("T"): 4,
        ord("C"): 4,
        ord("S"): 2,
        ord("P"): 1,
        ord("e"): 1,
        ord("E"): 5,
    }
    fields: dict[int, bytes] = {}
    position = 0
    while position < len(data):
        tag = data[position]
        position += 1
        if (width := widths.get(tag)) is None:
            continue  # Legacy app skips unrecognized single-byte tags/padding.
        if position + width > len(data):
            raise ProtocolError(f"Truncated feedback field {chr(tag)}")
        fields[tag] = bytes(data[position : position + width])
        position += width
    if not {ord("C"), ord("S"), ord("P")} <= fields.keys():
        raise ProtocolError("Feedback is missing the C/S/P fields")
    color = fields[ord("C")]
    sound = fields[ord("S")]
    flags = fields[ord("P")][0]
    return RestState(
        color=(color[0], color[1], color[2]),
        brightness=color[3],
        sound=sound[0],
        volume=sound[1],
        power=(flags & 0xC0) != 0xC0,
        raw_flags=flags,
    )


def _byte(value: int) -> str:
    if type(value) is not int or not 0 <= value <= 255:
        raise ValueError("Protocol values must be integers between 0 and 255")
    return f"{value:02X}"


def power_command(power: bool) -> bytes:
    """Set the device's master power."""
    return b"SI01" if power else b"SI00"


def color_command(color: tuple[int, int, int], brightness: int) -> bytes:
    """Set RGB and intensity together, avoiding intermediate stale colors."""
    if len(color) != 3:
        raise ValueError("RGB color must contain three channels")
    return ("SC" + "".join(_byte(v) for v in (*color, brightness))).encode("ascii")


def sound_command(sound: int) -> bytes:
    """Select a built-in sound; no audio is streamed."""
    return ("SN" + _byte(sound)).encode("ascii")


def volume_command(volume: int) -> bytes:
    """Set the sound's volume."""
    return ("SV" + _byte(volume)).encode("ascii")
