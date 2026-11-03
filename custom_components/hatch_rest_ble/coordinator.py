"""Keep all entity state tied to successful device feedback."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.components import bluetooth
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CommandBuilder, HatchRestClient
from .const import (
    CONF_LINGER_SECONDS,
    CONF_POLL_INTERVAL,
    DOMAIN,
    LINGER_SECONDS,
    MAX_LINGER_SECONDS,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_SECONDS,
)
from .protocol import HatchRestError, RestState

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .program_coordinator import HatchRestProgramCoordinator

_LOGGER = logging.getLogger(__name__)


def poll_interval_seconds(options: dict) -> int:
    """Saved interval, clamped to the current bounds (older releases allowed 5s)."""
    value = options.get(CONF_POLL_INTERVAL, POLL_INTERVAL_SECONDS)
    return int(min(max(value, MIN_POLL_INTERVAL_SECONDS), MAX_POLL_INTERVAL_SECONDS))


def linger_seconds(options: dict) -> int:
    """Saved post-command connection hold, clamped to the current bounds."""
    value = options.get(CONF_LINGER_SECONDS, LINGER_SECONDS)
    return int(min(max(value, 0), MAX_LINGER_SECONDS))


class HatchRestCoordinator(DataUpdateCoordinator[RestState]):
    """Poll periodically and publish only device-reported state after commands."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=poll_interval_seconds(entry.options)),
            always_update=False,
        )
        self.entry = entry
        self.address = entry.data[CONF_ADDRESS]
        self.device_name = entry.data.get(CONF_NAME, "Hatch Rest")
        self.client = HatchRestClient(hass, self.address, self.device_name)
        self.client.set_linger(linger_seconds(entry.options))
        self.client.set_state_listener(self._async_notified_state)
        self.program_coordinator: HatchRestProgramCoordinator | None = None
        self._last_manufacturer_data: dict[int, bytes] | None = None

    async def _async_update_data(self) -> RestState:
        try:
            return await self.client.async_update()
        except HatchRestError as err:
            raise UpdateFailed(str(err)) from err

    @callback
    def _async_notified_state(self, state: RestState) -> None:
        """Feedback pushed while a connection is held is confirmed device state."""
        self.async_set_updated_data(state)

    async def async_control(self, build_commands: CommandBuilder) -> None:
        """Run one atomic state-dependent operation and notify every entity."""
        reported = False

        def publish_state(state: RestState) -> None:
            nonlocal reported
            reported = True
            self.async_set_updated_data(state)

        try:
            state = await self.client.async_control(
                build_commands, on_state=publish_state
            )
        except (HatchRestError, ValueError) as err:
            self.async_set_update_error(UpdateFailed(str(err)))
            raise HomeAssistantError(f"Could not control Hatch Rest: {err}") from err
        if not reported:
            self.async_set_updated_data(state)

    @callback
    def async_start_bluetooth_tracking(self) -> list[CALLBACK_TYPE]:
        """Follow the Rest's advertisements; returns the unsubscribe callbacks."""
        return [
            bluetooth.async_register_callback(
                self.hass,
                self._async_advertisement,
                bluetooth.BluetoothCallbackMatcher(
                    address=self.address, connectable=True
                ),
                bluetooth.BluetoothScanningMode.PASSIVE,
            ),
            bluetooth.async_track_unavailable(
                self.hass, self._async_unavailable, self.address, connectable=True
            ),
        ]

    @callback
    def _async_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        manufacturer_data = dict(service_info.manufacturer_data)
        if manufacturer_data != self._last_manufacturer_data:
            # Recorded to learn whether advertisements carry device state.
            _LOGGER.debug(
                "%s: Advertisement data %s (rssi %s via %s)",
                self.address,
                {key: value.hex() for key, value in manufacturer_data.items()},
                service_info.rssi,
                service_info.source,
            )
            self._last_manufacturer_data = manufacturer_data
        if not self.last_update_success:
            # Back in range: refresh now instead of waiting for the next poll.
            self.hass.async_create_task(self.async_request_refresh(), eager_start=True)

    @callback
    def _async_unavailable(
        self, _service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        if self.client.connected:
            return  # A peripheral may stop advertising while we hold a link.
        if self.last_update_success:
            self.async_set_update_error(
                UpdateFailed("The Rest is no longer reachable over Bluetooth")
            )

    async def async_shutdown(self) -> None:
        """Stop coordinator work and release the connection on unload or HA stop."""
        if self.program_coordinator is not None:
            await self.program_coordinator.async_shutdown()
        await super().async_shutdown()
        await self.client.async_close()
