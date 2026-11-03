"""Set up the first-generation Hatch Rest Bluetooth integration."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import HatchRestCoordinator
from .program_coordinator import HatchRestProgramCoordinator

PLATFORMS = [Platform.LIGHT, Platform.MEDIA_PLAYER, Platform.SWITCH, Platform.BUTTON]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Rest after its first successful state read."""
    coordinator = HatchRestCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.program_coordinator = HatchRestProgramCoordinator(
        hass, entry, coordinator
    )

    # Whole-device power now belongs to the existing media player. Match the
    # old unique ID, including renamed/disabled entries, and only this entry.
    registry = er.async_get(hass)
    old_power_entity_id = registry.async_get_entity_id(
        Platform.SWITCH, DOMAIN, f"{entry.data[CONF_ADDRESS].upper()}:power"
    )
    if (
        old_power_entity_id is not None
        and (old_power_entry := registry.async_get(old_power_entity_id)) is not None
        and old_power_entry.config_entry_id == entry.entry_id
    ):
        registry.async_remove(old_power_entity_id)

    # DataUpdateCoordinator registers entry-unload cleanup when passed the entry.
    # HA stopping does not unload entries, so also close the transport on stop.
    async def _async_stop(_event: Event) -> None:
        await coordinator.async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_create_background_task(
        hass,
        coordinator.program_coordinator.async_refresh(),
        "Read Hatch Rest programs",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload platforms; entry callbacks then shut down the coordinator."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Allow a removed Rest to be discovered again without restarting HA."""
    bluetooth.async_rediscover_address(hass, entry.data[CONF_ADDRESS])
