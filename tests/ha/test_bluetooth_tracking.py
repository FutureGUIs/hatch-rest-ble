"""Advertisement-driven availability, pushed feedback, and signal strength."""

from dataclasses import replace
from time import monotonic

import pytest
from habluetooth import get_manager
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.hatch_rest_ble.const import CONF_LINGER_SECONDS, DOMAIN
from custom_components.hatch_rest_ble.protocol import HatchRestError

from .conftest import ADDRESS, make_service_info

pytestmark = pytest.mark.asyncio


def advertise(**kwargs) -> None:
    """Deliver an advertisement through HA's real Bluetooth manager."""
    info = make_service_info(address=ADDRESS, **kwargs)
    info.time = monotonic()
    get_manager().scanner_adv_received(info)


async def _setup(hass, config_entry, mock_client):
    mock_client.connected = False
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry.runtime_data


async def test_returning_advertisement_refreshes_failed_device(
    hass, config_entry, mock_client, rest_state
):
    coordinator = await _setup(hass, config_entry, mock_client)
    mock_client.async_update.side_effect = HatchRestError("out of range")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    calls = mock_client.async_update.await_count

    mock_client.async_update.side_effect = None
    mock_client.async_update.return_value = rest_state
    advertise()
    await hass.async_block_till_done()
    assert mock_client.async_update.await_count == calls + 1
    assert coordinator.last_update_success


async def test_advertisements_do_not_poll_a_healthy_device(
    hass, config_entry, mock_client
):
    await _setup(hass, config_entry, mock_client)
    calls = mock_client.async_update.await_count
    advertise()
    advertise(manufacturer_data={1076: b"r"})
    await hass.async_block_till_done()
    assert mock_client.async_update.await_count == calls


async def test_lost_advertisements_mark_entities_unavailable(
    hass, config_entry, mock_client
):
    coordinator = await _setup(hass, config_entry, mock_client)
    coordinator._async_unavailable(make_service_info(address=ADDRESS))
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert hass.states.get("light.nursery_rest_light").state == STATE_UNAVAILABLE


async def test_lost_advertisements_ignored_while_connected(
    hass, config_entry, mock_client
):
    coordinator = await _setup(hass, config_entry, mock_client)
    mock_client.connected = True
    coordinator._async_unavailable(make_service_info(address=ADDRESS))
    assert coordinator.last_update_success


async def test_hold_option_and_pushed_feedback_reach_entities(
    hass, config_entry, mock_client, rest_state
):
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_LINGER_SECONDS: 30}
    )
    coordinator = await _setup(hass, config_entry, mock_client)
    mock_client.set_linger.assert_called_once_with(30)
    listener = mock_client.set_state_listener.call_args.args[0]
    listener(replace(rest_state, power=False))
    await hass.async_block_till_done()
    assert coordinator.data.power is False
    assert hass.states.get("media_player.nursery_rest_sound").state == "off"


async def test_firmware_revision_in_device_registry(
    hass, config_entry, mock_client, rest_state
):
    mock_client.async_update.return_value = replace(
        rest_state, hardware_version="2.0", firmware_version="1.0.4.31"
    )
    await _setup(hass, config_entry, mock_client)
    (device,) = dr.async_entries_for_config_entry(
        dr.async_get(hass), config_entry.entry_id
    )
    assert device.sw_version == "1.0.4.31"
    assert device.hw_version == "2.0"


async def test_signal_strength_sensor_follows_advertisements(
    hass, config_entry, mock_client
):
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{ADDRESS}:rssi",
        config_entry=config_entry,
        suggested_object_id="nursery_rest_signal_strength",
    )
    await _setup(hass, config_entry, mock_client)
    advertise(rssi=-71, source="AA:00:00:00:00:01")
    await hass.async_block_till_done()
    state = hass.states.get(entry.entity_id)
    assert state.state == "-71"
    assert state.attributes["unit_of_measurement"] == "dBm"
    assert state.attributes["source"] == "AA:00:00:00:00:01"

    sensor = hass.data["entity_components"]["sensor"].get_entity(entry.entity_id)
    sensor._async_unavailable(make_service_info(address=ADDRESS))
    assert hass.states.get(entry.entity_id).state == STATE_UNAVAILABLE


async def test_signal_strength_sensor_is_disabled_by_default(
    hass, config_entry, mock_client
):
    await _setup(hass, config_entry, mock_client)
    entry = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}:rssi")
    assert er.async_get(hass).async_get(entry).disabled_by is (
        er.RegistryEntryDisabler.INTEGRATION
    )
