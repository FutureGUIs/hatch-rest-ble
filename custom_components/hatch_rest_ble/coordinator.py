"""Keep all entity state tied to successful device feedback."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CommandBuilder, HatchRestClient
from .const import CONF_POLL_INTERVAL, DOMAIN, POLL_INTERVAL_SECONDS
from .protocol import HatchRestError, RestState

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .program_coordinator import HatchRestProgramCoordinator

_LOGGER = logging.getLogger(__name__)


class HatchRestCoordinator(DataUpdateCoordinator[RestState]):
    """Poll periodically and publish only device-reported state after commands."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_POLL_INTERVAL, POLL_INTERVAL_SECONDS)
            ),
            always_update=False,
        )
        self.entry = entry
        self.address = entry.data[CONF_ADDRESS]
        self.device_name = entry.data.get(CONF_NAME, "Hatch Rest")
        self.client = HatchRestClient(hass, self.address, self.device_name)
        self.program_coordinator: HatchRestProgramCoordinator | None = None

    async def _async_update_data(self) -> RestState:
        try:
            return await self.client.async_update()
        except HatchRestError as err:
            raise UpdateFailed(str(err)) from err

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

    async def async_shutdown(self) -> None:
        """Stop coordinator work and release the connection on unload or HA stop."""
        if self.program_coordinator is not None:
            await self.program_coordinator.async_shutdown()
        await super().async_shutdown()
        await self.client.async_close()
