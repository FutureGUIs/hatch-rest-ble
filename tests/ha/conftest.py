"""Real Home Assistant fixtures, with only the device transport mocked."""

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hatch_rest_ble.api import HatchRestClient
from custom_components.hatch_rest_ble.const import DOMAIN, MANUFACTURER_ID
from custom_components.hatch_rest_ble.programs import ProgramSnapshot
from custom_components.hatch_rest_ble.protocol import RestState

ADDRESS = "AA:BB:CC:DD:EE:FF"
NAME = "Nursery Rest"


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations: None) -> None:
    """Let HA load the integration from custom_components."""


@pytest.fixture(autouse=True)
def bluetooth_stack(enable_bluetooth: None) -> None:
    """Use HA's real Bluetooth manager with its official scanner fixtures."""


@pytest.fixture
def rest_state() -> RestState:
    return RestState(
        color=(255, 128, 64),
        brightness=128,
        sound=3,
        volume=100,
        power=True,
        raw_flags=0,
    )


def make_service_info(
    *,
    manufacturer_data: dict[int, bytes] | None = None,
    connectable: bool = True,
    name: str = NAME,
) -> BluetoothServiceInfoBleak:
    """Build a real service-info object, including actual Bleak wrappers."""
    address = ADDRESS.lower()
    if manufacturer_data is None:
        manufacturer_data = {MANUFACTURER_ID: b"R"}
    advertisement = AdvertisementData(
        local_name=name,
        manufacturer_data=manufacturer_data,
        service_data={},
        service_uuids=[],
        tx_power=None,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data=manufacturer_data,
        service_data={},
        service_uuids=[],
        source="local",
        device=BLEDevice(address, name, {}),
        advertisement=advertisement,
        connectable=connectable,
        time=0,
        tx_power=None,
    )


@pytest.fixture
def service_info() -> BluetoothServiceInfoBleak:
    return make_service_info()


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=NAME,
        unique_id=ADDRESS,
        data={CONF_ADDRESS: ADDRESS, CONF_NAME: NAME},
    )


@pytest.fixture
def mock_client(rest_state: RestState) -> Generator[MagicMock]:
    """Mock the boundary to BLE while keeping HA lifecycle code intact."""
    client = MagicMock(spec=HatchRestClient)
    client.async_update = AsyncMock(return_value=rest_state)
    client.async_control = AsyncMock(return_value=rest_state)
    client.async_close = AsyncMock()
    client.async_get_programs = AsyncMock(return_value=ProgramSnapshot(programs=()))
    client.async_save_program = AsyncMock()
    client.async_set_program_enabled = AsyncMock()
    client.async_delete_program = AsyncMock()
    with (
        patch(
            "custom_components.hatch_rest_ble.config_flow.HatchRestClient",
            return_value=client,
        ),
        patch(
            "custom_components.hatch_rest_ble.coordinator.HatchRestClient",
            return_value=client,
        ),
    ):
        yield client


@pytest.fixture
def mock_flow_setup() -> Generator[AsyncMock]:
    """Avoid setting up entities while testing configuration entry creation."""
    with patch(
        "custom_components.hatch_rest_ble.async_setup_entry", return_value=True
    ) as setup:
        yield setup
