"""Diagnostic Bluetooth signal strength for troubleshooting range and routing."""

from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import HatchRestCoordinator
from .entity import HatchRestEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the disabled-by-default signal strength sensor."""
    async_add_entities([HatchRestSignalStrength(entry.runtime_data)])


class HatchRestSignalStrength(HatchRestEntity, SensorEntity):
    """RSSI of the latest advertisement, and which adapter or proxy heard it.

    Updated from advertisements rather than GATT sessions, so it keeps working
    while polls fail - which is when it is most useful.
    """

    _attr_name = "Signal strength"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: HatchRestCoordinator) -> None:
        super().__init__(coordinator, "rssi")
        self._source: str | None = None

    @property
    def available(self) -> bool:
        return self._attr_native_value is not None

    @property
    def extra_state_attributes(self) -> dict[str, str | None]:
        return {"source": self._source}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        address = self.coordinator.address
        if info := bluetooth.async_last_service_info(
            self.hass, address, connectable=False
        ):
            self._attr_native_value = info.rssi
            self._source = info.source
        self.async_on_remove(
            bluetooth.async_register_callback(
                self.hass,
                self._async_advertisement,
                bluetooth.BluetoothCallbackMatcher(address=address),
                bluetooth.BluetoothScanningMode.PASSIVE,
            )
        )
        self.async_on_remove(
            bluetooth.async_track_unavailable(
                self.hass, self._async_unavailable, address, connectable=False
            )
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Signal strength comes from advertisements, not device feedback."""

    @callback
    def _async_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        _change: bluetooth.BluetoothChange,
    ) -> None:
        if (
            service_info.rssi == self._attr_native_value
            and service_info.source == self._source
        ):
            return
        self._attr_native_value = service_info.rssi
        self._source = service_info.source
        self.async_write_ha_state()

    @callback
    def _async_unavailable(
        self, _service_info: bluetooth.BluetoothServiceInfoBleak
    ) -> None:
        self._attr_native_value = None
        self.async_write_ha_state()
