"""Control commands using the Rest's native shared-power behavior."""

from __future__ import annotations

from .protocol import (
    RestState,
    color_command,
    power_command,
    sound_command,
)


def power_commands(state: RestState, power: bool) -> list[bytes]:
    """Leave whole-device settings restoration entirely to the firmware."""
    return [power_command(power)] if state.power != power else []


def light_on_commands(
    state: RestState, color: tuple[int, int, int], brightness: int
) -> list[bytes]:
    """Wake normally, then apply only the requested lamp settings."""
    commands = []
    if brightness > 0 and not state.power:
        commands.append(power_command(True))
    commands.append(color_command(color, brightness))
    return commands


def sound_on_commands(
    state: RestState, sound: int, *, force: bool = False
) -> list[bytes]:
    """Wake normally, then select sound without changing lamp or volume."""
    if sound and not state.power:
        return [power_command(True), sound_command(sound)]
    return [sound_command(sound)] if force or sound != state.sound else []
