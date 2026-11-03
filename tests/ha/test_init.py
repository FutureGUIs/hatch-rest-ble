"""Verify actual Home Assistant setup, failure, shutdown, and unload lifecycle."""

from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import entity_registry as er

from custom_components.hatch_rest_ble.protocol import HatchRestError

from .conftest import ADDRESS

pytestmark = pytest.mark.asyncio


async def test_setup_and_unload(hass, config_entry, mock_client, rest_state):
    """Initial device feedback is shared by all platforms before publication."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state == ConfigEntryState.LOADED
    assert config_entry.runtime_data.data == rest_state
    entities = er.async_entries_for_config_entry(
        er.async_get(hass), config_entry.entry_id
    )
    assert len(entities) == 4
    assert {entity.entity_id.split(".", 1)[0] for entity in entities} == {
        "light",
        "media_player",
        "button",
        "sensor",
    }
    assert {entity.unique_id for entity in entities} == {
        f"{ADDRESS}:light",
        f"{ADDRESS}:sound",
        f"{ADDRESS}:refresh_programs",
        f"{ADDRESS}:rssi",
    }
    enabled = [entity for entity in entities if entity.disabled_by is None]
    assert {entity.unique_id for entity in enabled} == {
        f"{ADDRESS}:light",
        f"{ADDRESS}:sound",
        f"{ADDRESS}:refresh_programs",
    }
    assert all(hass.states.get(entity.entity_id) is not None for entity in enabled)
    mock_client.async_update.assert_awaited_once()
    mock_client.async_control.assert_not_awaited()
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.async_close.assert_awaited_once()


async def test_setup_retries_failed_first_read(hass, config_entry, mock_client):
    """A failed initial read never publishes an apparently-working Rest."""
    mock_client.async_update.side_effect = HatchRestError("out of range")
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state == ConfigEntryState.SETUP_RETRY
    mock_client.async_close.assert_awaited_once()


async def test_home_assistant_stop_closes_transport(hass, config_entry, mock_client):
    """HA stop releases the transport even though entries are not unloaded."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    mock_client.async_close.assert_awaited_once()


async def test_removal_triggers_rediscovery(hass, config_entry, mock_client):
    """Removing an entry allows the same MAC to be discovered again."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    with patch(
        "custom_components.hatch_rest_ble.bluetooth.async_rediscover_address"
    ) as rediscover:
        await hass.config_entries.async_remove(config_entry.entry_id)
        await hass.async_block_till_done()
    rediscover.assert_called_once_with(hass, ADDRESS)
    mock_client.async_close.assert_awaited_once()
