"""Color and brightness control for a first generation Rest."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    EFFECT_OFF,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import COLOR_GRADIENT
from .control import light_on_commands
from .coordinator import HatchRestCoordinator
from .entity import HatchRestEntity
from .protocol import RestState, color_command

EFFECT_COLOR_LOOP = "Color loop"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the light after the coordinator's initial refresh."""
    async_add_entities([HatchRestLight(entry.runtime_data)])


class HatchRestLight(HatchRestEntity, LightEntity):
    """Control the lamp while using the Rest's native wake behavior."""

    _attr_name = "Light"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, coordinator: HatchRestCoordinator) -> None:
        """Remember a usable brightness for a later lamp turn on."""
        super().__init__(coordinator, "light")
        self._attr_supported_color_modes = {ColorMode.RGB}
        self._attr_effect_list = [EFFECT_OFF, EFFECT_COLOR_LOOP]
        self._last_brightness = coordinator.data.brightness or 255

    @property
    def brightness(self) -> int:
        """Return the lamp's current 0..255 brightness."""
        return self.coordinator.data.brightness

    @property
    def is_on(self) -> bool:
        """A powered device with a dark lamp has an off light entity."""
        data = self.coordinator.data
        return data.power and data.brightness > 0

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """The gradient marker is an effect, not an actual RGB color."""
        color = self.coordinator.data.color
        return None if color == COLOR_GRADIENT else color

    @property
    def effect(self) -> str:
        """Report the Rest's built-in color loop."""
        return (
            EFFECT_COLOR_LOOP
            if self.coordinator.data.color == COLOR_GRADIENT
            else EFFECT_OFF
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Remember brightness changed by the app or the device's controls."""
        if brightness := self.coordinator.data.brightness:
            self._last_brightness = brightness
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Set color and brightness together from the latest device state."""
        requested_brightness = kwargs.get(ATTR_BRIGHTNESS)
        requested_color = kwargs.get(ATTR_RGB_COLOR)
        requested_effect = kwargs.get(ATTR_EFFECT)
        if requested_brightness is not None and (
            not isinstance(requested_brightness, int)
            or isinstance(requested_brightness, bool)
            or not 0 <= requested_brightness <= 255
        ):
            raise ServiceValidationError("Brightness must be an integer from 0 to 255")
        if requested_color is not None:
            if (
                not isinstance(requested_color, (tuple, list))
                or len(requested_color) != 3
                or any(
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not 0 <= value <= 255
                    for value in requested_color
                )
            ):
                raise ServiceValidationError(
                    "RGB color must contain three integers from 0 to 255"
                )
            requested_color = tuple(requested_color)
        if requested_effect is not None and requested_effect not in self.effect_list:
            raise ServiceValidationError(
                f"Unsupported effect: {requested_effect}. Choose {EFFECT_OFF} or "
                f"{EFFECT_COLOR_LOOP}"
            )

        def commands(state: RestState) -> list[bytes]:
            if state.brightness:
                self._last_brightness = state.brightness
            brightness = (
                requested_brightness
                if requested_brightness is not None
                else state.brightness or self._last_brightness
            )
            color = requested_color if requested_color is not None else state.color
            if requested_effect == EFFECT_COLOR_LOOP:
                color = COLOR_GRADIENT
            elif requested_effect == EFFECT_OFF and color == COLOR_GRADIENT:
                color = (255, 255, 255)
            return light_on_commands(state, color, brightness)

        await self.coordinator.async_control(commands)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Darken only the lamp, retaining the current color and sound."""

        def commands(state: RestState) -> list[bytes]:
            if state.brightness:
                self._last_brightness = state.brightness
            return [color_command(state.color, 0)] if state.brightness else []

        await self.coordinator.async_control(commands)
