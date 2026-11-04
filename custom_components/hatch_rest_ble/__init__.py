"""Set up the first-generation Hatch Rest Bluetooth integration."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import HatchRestCoordinator
from .program_coordinator import HatchRestProgramCoordinator
from .programs import ProgramConflictError, program_slot_from_key

PLATFORMS = [
    Platform.LIGHT,
    Platform.MEDIA_PLAYER,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.TEXT,
    Platform.TIME,
    Platform.NUMBER,
    Platform.SELECT,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a Rest after its first successful state read."""
    coordinator = HatchRestCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    coordinator.program_coordinator = HatchRestProgramCoordinator(
        hass, entry, coordinator
    )
    # Register the Rest itself first: program devices are its children.
    address = entry.data[CONF_ADDRESS]
    coordinator.program_coordinator.hub_device_id = (
        dr.async_get(hass)
        .async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            manufacturer="Hatch",
            model="Rest (1st generation)",
            name=coordinator.device_name,
        )
        .id
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
    for unsubscribe in coordinator.async_start_bluetooth_tracking():
        entry.async_on_unload(unsubscribe)
    programs = coordinator.program_coordinator
    entry.async_on_unload(programs.async_add_listener(programs.async_sync_registry))
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


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: dr.DeviceEntry | dr.ChildDeviceEntry,
) -> bool:
    """Deleting a program device deletes that program from the Rest first."""
    address = entry.data[CONF_ADDRESS]
    slots = {
        program_slot_from_key(address, value)
        for domain, value in device.identifiers
        if domain == DOMAIN
    } - {None}
    if not slots:
        raise HomeAssistantError(
            "To remove the Rest itself, delete its integration entry instead"
        )
    slot = slots.pop()
    if entry.state is not ConfigEntryState.LOADED:
        raise HomeAssistantError(
            "The Rest must be connected to delete its programs; "
            "reload the integration and try again"
        )
    programs = entry.runtime_data.program_coordinator
    program = programs.data.get(slot) if programs.data is not None else None
    if program is None:
        return True  # Already gone from the last complete read.
    try:
        await programs.async_delete_program(slot, expected=program)
    except ProgramConflictError as err:
        entry.async_create_background_task(
            hass, programs.async_refresh(), "Read Hatch Rest programs"
        )
        raise HomeAssistantError(
            f"Program {slot} was changed on the Rest, for example in the Hatch "
            "app. The latest version is being loaded; delete it again if you "
            "still want to."
        ) from err
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Allow a removed Rest to be discovered again without restarting HA."""
    bluetooth.async_rediscover_address(hass, entry.data[CONF_ADDRESS])
