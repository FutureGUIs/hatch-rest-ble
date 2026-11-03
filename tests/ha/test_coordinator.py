"""Exercise coordinator error semantics against the real HA coordinator."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hatch_rest_ble.const import (
    CONF_POLL_INTERVAL,
    POLL_INTERVAL_SECONDS,
)
from custom_components.hatch_rest_ble.coordinator import HatchRestCoordinator
from custom_components.hatch_rest_ble.media_player import HatchRestMediaPlayer
from custom_components.hatch_rest_ble.protocol import HatchRestError

pytestmark = pytest.mark.asyncio


async def test_failed_poll_keeps_values_but_marks_unavailable(
    hass, config_entry, mock_client, rest_state
):
    """Cached values survive a failed refresh while success/availability changes."""
    coordinator = HatchRestCoordinator(hass, config_entry)
    try:
        await coordinator.async_refresh()
        assert coordinator.data == rest_state
        assert coordinator.last_update_success
        mock_client.async_update.side_effect = HatchRestError("connection lost")
        await coordinator.async_refresh()
        assert coordinator.data == rest_state
        assert not coordinator.last_update_success
        assert isinstance(coordinator.last_exception, UpdateFailed)
    finally:
        await coordinator.async_shutdown()


@pytest.mark.parametrize(
    "error", [HatchRestError("write failed"), ValueError("bad RGB")]
)
async def test_failed_command_raises_action_error(
    hass, config_entry, mock_client, rest_state, error
):
    """Action failures reach callers and do not become optimistic successes."""
    coordinator = HatchRestCoordinator(hass, config_entry)
    coordinator.async_set_updated_data(rest_state)
    mock_client.async_control.side_effect = error
    try:
        with pytest.raises(HomeAssistantError, match="Could not control Hatch Rest"):
            await coordinator.async_control(lambda _state: [b"SI01"])
        assert coordinator.data == rest_state
        assert not coordinator.last_update_success
        assert isinstance(coordinator.last_exception, UpdateFailed)
    finally:
        await coordinator.async_shutdown()


async def test_successful_action_publishes_readback(hass, config_entry, mock_client):
    """Only the transport's returned feedback is published to subscribers."""
    coordinator = HatchRestCoordinator(hass, config_entry)
    try:
        await coordinator.async_control(lambda _state: [b"SI01"])
        assert coordinator.data == mock_client.async_control.return_value
        assert coordinator.last_update_success
    finally:
        await coordinator.async_shutdown()
    mock_client.async_close.assert_awaited_once()


async def test_failed_scheduled_poll_recovers_on_next_interval(
    hass, config_entry, mock_client, rest_state
):
    """A transient failure must not require an action or an integration reload."""
    coordinator = HatchRestCoordinator(hass, config_entry)
    await coordinator.async_refresh()
    player = HatchRestMediaPlayer(coordinator)
    listener = Mock()
    remove_listener = coordinator.async_add_listener(listener)
    mock_client.async_update.side_effect = [
        HatchRestError("Invalid handle 1"),
        rest_state,
    ]
    try:
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=POLL_INTERVAL_SECONDS + 1)
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert mock_client.async_update.await_count == 2
        assert coordinator.data == rest_state
        assert not player.available
        listener.assert_called_once_with()

        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=POLL_INTERVAL_SECONDS + 1)
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert mock_client.async_update.await_count == 3
        assert coordinator.data == rest_state
        assert player.available
        # The state is unchanged, but recovering availability still updates HA.
        assert listener.call_count == 2
        mock_client.async_control.assert_not_awaited()
    finally:
        remove_listener()
        await coordinator.async_shutdown()


async def test_failed_action_recovers_on_next_scheduled_poll(
    hass, config_entry, mock_client, rest_state
):
    """An action error preserves the timer that later restores availability."""
    coordinator = HatchRestCoordinator(hass, config_entry)
    await coordinator.async_refresh()
    player = HatchRestMediaPlayer(coordinator)
    remove_listener = coordinator.async_add_listener(Mock())
    mock_client.async_control.side_effect = HatchRestError("Invalid handle 1")
    try:
        with pytest.raises(HomeAssistantError):
            await coordinator.async_control(lambda _state: [b"SI01"])
        assert not player.available
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=POLL_INTERVAL_SECONDS + 1)
        )
        await hass.async_block_till_done(wait_background_tasks=True)
        assert mock_client.async_update.await_count == 2
        assert coordinator.data == rest_state
        assert player.available
    finally:
        remove_listener()
        await coordinator.async_shutdown()


async def test_confirmed_command_state_publishes_before_cleanup_completes(
    hass, config_entry, mock_client, rest_state
):
    """Entity listeners receive feedback while the transport finishes teardown."""
    coordinator = HatchRestCoordinator(hass, config_entry)
    coordinator.async_set_updated_data(rest_state)
    listener = Mock()
    remove_listener = coordinator.async_add_listener(listener)
    confirmed = replace(rest_state, sound=7)
    published, released = asyncio.Event(), asyncio.Event()

    async def control(build_commands, *, on_state):
        on_state(confirmed)
        published.set()
        await released.wait()
        return confirmed

    mock_client.async_control.side_effect = control
    action = asyncio.create_task(coordinator.async_control(lambda _state: [b"SN07"]))
    try:
        await asyncio.wait_for(published.wait(), 1)
        assert coordinator.data == confirmed
        assert coordinator.last_update_success
        listener.assert_called_once_with()
        assert not action.done()
        mock_client.async_update.assert_not_awaited()
        released.set()
        await action
        listener.assert_called_once_with()
    finally:
        released.set()
        await action
        remove_listener()
        await coordinator.async_shutdown()


@pytest.mark.parametrize(
    ("interval", "expected"),
    [(None, POLL_INTERVAL_SECONDS), (5, 15), (15, 15), (300, 300), (900, 300)],
)
async def test_configured_poll_interval(
    hass, config_entry, mock_client, interval, expected
):
    """Entry options set the timer; older out-of-range values are clamped."""
    if interval is not None:
        config_entry.add_to_hass(hass)
        hass.config_entries.async_update_entry(
            config_entry, options={CONF_POLL_INTERVAL: interval}
        )
    coordinator = HatchRestCoordinator(hass, config_entry)
    try:
        assert coordinator.update_interval == timedelta(seconds=expected)
    finally:
        await coordinator.async_shutdown()
