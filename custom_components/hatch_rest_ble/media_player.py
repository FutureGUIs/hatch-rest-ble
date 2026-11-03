"""Built-in sounds and volume for a first generation Hatch Rest."""

from __future__ import annotations

import math

from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import SOUNDS
from .control import power_commands, sound_on_commands
from .coordinator import HatchRestCoordinator
from .entity import HatchRestEntity
from .protocol import RestState, sound_command, volume_command

DEFAULT_SOUND = 3


def _sound_name(sound: int) -> str:
    """Display undocumented sound IDs without discarding device state."""
    return SOUNDS.get(sound, f"Unknown sound ({sound})")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add a player for the Rest's built-in sounds."""
    async_add_entities([HatchRestMediaPlayer(entry.runtime_data)])


class HatchRestMediaPlayer(HatchRestEntity, MediaPlayerEntity):
    """Select internal sounds; the Rest cannot play URLs or uploaded audio."""

    _attr_name = "Sound"
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_media_content_type = MediaType.MUSIC
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.SELECT_SOURCE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: HatchRestCoordinator) -> None:
        """Retain only nonzero sounds as playback resumes after a pause."""
        super().__init__(coordinator, "sound")
        self._previous_sound: int | None = coordinator.data.sound or None

    @property
    def state(self) -> MediaPlayerState:
        """Report whole-device power even when the lamp is the only output."""
        data = self.coordinator.data
        if not data.power:
            return MediaPlayerState.OFF
        return MediaPlayerState.PLAYING if data.sound else MediaPlayerState.IDLE

    @property
    def source(self) -> str:
        """Return a readable name even for undocumented sound IDs."""
        return _sound_name(self.coordinator.data.sound)

    @property
    def source_list(self) -> list[str]:
        """List known sounds and any undocumented sound currently selected."""
        sources = list(SOUNDS.values())
        if (sound := self.coordinator.data.sound) not in SOUNDS:
            sources.append(_sound_name(sound))
        return sources

    @property
    def media_title(self) -> str | None:
        """Show the currently selected sound as the playing title."""
        sound = self.coordinator.data.sound
        return _sound_name(sound) if sound else None

    @property
    def volume_level(self) -> float:
        """Zero volume is a valid level, never an unknown value."""
        data = self.coordinator.data
        return max(0, (data.volume - data.volume_min) / (255 - data.volume_min))

    @callback
    def _handle_coordinator_update(self) -> None:
        """Also remember sounds selected outside Home Assistant."""
        if sound := self.coordinator.data.sound:
            self._previous_sound = sound
        super()._handle_coordinator_update()

    async def async_set_volume_level(self, volume: float) -> None:
        """Set a finite fractional volume level in the 0..1 range."""
        if (
            not isinstance(volume, (int, float))
            or isinstance(volume, bool)
            or not math.isfinite(volume)
            or not 0 <= volume <= 1
        ):
            raise ServiceValidationError("Volume must be a number from 0 to 1")

        def commands(state: RestState) -> list[bytes]:
            # Match Android Math.round rather than Python's ties-to-even rounding.
            raw_volume = (
                math.floor(volume * (255 - state.volume_min) + 0.5) + state.volume_min
            )
            return [volume_command(raw_volume)]

        await self.coordinator.async_control(commands)

    async def async_select_source(self, source: str) -> None:
        """Select a known sound or the device's observed undocumented sound."""
        sources = {name: sound for sound, name in SOUNDS.items()}
        current_sound = self.coordinator.data.sound
        if current_sound not in SOUNDS:
            sources[_sound_name(current_sound)] = current_sound
        if source not in sources:
            raise ServiceValidationError(
                f"Unknown sound source: {source}. Choose a source from the source list"
            )
        sound = sources[source]

        def commands(state: RestState) -> list[bytes]:
            if state.sound:
                self._previous_sound = state.sound
            return sound_on_commands(state, sound, force=True)

        await self.coordinator.async_control(commands)
        if self.coordinator.data.sound:
            self._previous_sound = self.coordinator.data.sound

    async def async_media_pause(self) -> None:
        """Silence the sound while preserving lamp settings and resume sound."""

        def commands(state: RestState) -> list[bytes]:
            if state.sound:
                self._previous_sound = state.sound
                return [sound_command(0)]
            return []

        await self.coordinator.async_control(commands)

    async def async_media_play(self) -> None:
        """Resume the last sound, falling back to pink noise after a restart."""

        def commands(state: RestState) -> list[bytes]:
            sound = state.sound or self._previous_sound or DEFAULT_SOUND
            return sound_on_commands(state, sound)

        await self.coordinator.async_control(commands)
        if self.coordinator.data.sound:
            self._previous_sound = self.coordinator.data.sound

    async def async_turn_on(self) -> None:
        """Use native whole-device power on, letting the Rest restore settings."""
        await self.coordinator.async_control(lambda state: power_commands(state, True))

    async def async_turn_off(self) -> None:
        """Use native whole-device power off for both sound and light."""
        await self.coordinator.async_control(lambda state: power_commands(state, False))
