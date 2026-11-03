"""Shared Home Assistant entity for a first generation Hatch Rest."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HatchRestCoordinator


class HatchRestEntity(CoordinatorEntity[HatchRestCoordinator]):
    """Represent one control on the Rest's shared BLE connection."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HatchRestCoordinator, suffix: str) -> None:
        """Group every control under the same device and full BLE address."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}:{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer="Hatch",
            model="Rest (1st generation)",
            name=coordinator.device_name,
            hw_version=coordinator.data.hardware_version,
            sw_version=coordinator.data.firmware_version,
        )
