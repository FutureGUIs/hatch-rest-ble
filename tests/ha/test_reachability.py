"""Unreachable Rest: actionable errors and prompt setup retry."""

from time import monotonic
from types import SimpleNamespace

import pytest
from habluetooth import get_manager
from homeassistant.components.bluetooth.match import IntegrationMatcher
from homeassistant.config_entries import ConfigEntryState
from homeassistant.loader import async_get_bluetooth

from custom_components.hatch_rest_ble import api
from custom_components.hatch_rest_ble.protocol import HatchRestError

from .conftest import ADDRESS, NAME, make_service_info

pytestmark = pytest.mark.asyncio


async def test_never_heard_points_at_app_power_and_range(hass):
    client = api.HatchRestClient(hass, ADDRESS, NAME)
    with pytest.raises(HatchRestError) as err:
        client._get_device()
    message = str(err.value)
    assert "close it" in message
    assert "one connection at a time" in message
    assert "Bluetooth: " in message  # HA's own reachability detail follows.


async def test_passive_only_points_at_connectable_scanner(hass, monkeypatch):
    passive = SimpleNamespace(scanner=SimpleNamespace(connectable=False))
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_scanner_devices_by_address",
        lambda *args, **kwargs: [passive],
    )
    client = api.HatchRestClient(hass, ADDRESS, NAME)
    with pytest.raises(HatchRestError, match="cannot make connections"):
        client._get_device()


async def test_diagnostics_failure_still_explains(hass, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_address_reachability_diagnostics",
        broken,
    )
    client = api.HatchRestClient(hass, ADDRESS, NAME)
    with pytest.raises(HatchRestError, match="close it") as err:
        client._get_device()
    assert "Bluetooth: " not in str(err.value)


@pytest.fixture
async def live_matcher(hass):
    """Match this integration's manifest like a real HA startup does.

    The test harness starts Bluetooth before custom integrations are
    loaded, so its matcher has no entry for this domain.
    """
    manager = get_manager()
    matcher = IntegrationMatcher(await async_get_bluetooth(hass))
    matcher.async_setup()
    original = manager._integration_matcher
    manager._integration_matcher = matcher
    yield manager
    manager._integration_matcher = original


def advertise(manager) -> None:
    info = make_service_info(address=ADDRESS)
    info.time = monotonic()
    manager.scanner_adv_received(info)


async def test_setup_retries_as_soon_as_the_rest_advertises(
    hass, config_entry, mock_client, rest_state, live_matcher
):
    """HA reloads a retrying entry when Bluetooth rediscovers its device."""
    mock_client.async_update.side_effect = [
        HatchRestError("not advertising"),
        HatchRestError("still not reachable"),
        rest_state,
    ]
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    advertise(live_matcher)  # First sighting: discovery aborts and reloads.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_client.async_update.await_count == 2
    assert config_entry.state is ConfigEntryState.SETUP_RETRY

    advertise(live_matcher)  # Already discovered: no extra reload.
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_client.async_update.await_count == 2

    # After HA marks the Rest gone (e.g. while the app held it), its return
    # triggers discovery, and so a reload, again. Mirror what HA's
    # unavailable check does: drop it from history and from match history.
    live_matcher._connectable_history.pop(ADDRESS, None)
    live_matcher._all_history.pop(ADDRESS, None)
    live_matcher._address_disappeared(ADDRESS)
    advertise(live_matcher)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_client.async_update.await_count == 3
    assert config_entry.state is ConfigEntryState.LOADED
