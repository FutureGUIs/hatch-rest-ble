"""Device program entities, verified state, failures, and lifecycle."""

import asyncio
from dataclasses import replace
from datetime import datetime

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.hatch_rest_ble.const import DOMAIN
from custom_components.hatch_rest_ble.programs import (
    ProgramConflictError,
    ProgramSnapshot,
    RestProgram,
)
from custom_components.hatch_rest_ble.protocol import HatchRestError

from .conftest import ADDRESS

pytestmark = pytest.mark.asyncio


@pytest.fixture
def program():
    return RestProgram(
        slot=1,
        name="Bedtime",
        start=datetime(2026, 10, 15, 19, 30),
        duration=41400,
        days=0x7F,
        color=(255, 100, 10),
        brightness=50,
        sound=7,
        volume=64,
        flags=0xC0,
        buttons=0x01FF,
    )


async def setup_programs(hass, config_entry, mock_client, programs):
    mock_client.async_get_programs.return_value = ProgramSnapshot(programs=programs)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return config_entry.runtime_data.program_coordinator


def entity_id(hass, suffix):
    domain = "button" if suffix == "refresh_programs" else "switch"
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{ADDRESS}:{suffix}")


async def test_program_switches_share_device_and_report_timing(
    hass, config_entry, mock_client, program
):
    await setup_programs(hass, config_entry, mock_client, (program,))
    registry = er.async_get(hass)
    switch_id = entity_id(hass, "program:1")
    state = hass.states.get(switch_id)
    assert state.state == "on"
    assert state.attributes["start_time"] == "19:30:00"
    assert state.attributes["calculated_end_time"] == "07:00:00"
    assert state.attributes["ends_next_day"]
    assert state.attributes["duration_seconds"] == 41400
    assert state.attributes["repeat_days"] == [
        "Sunday",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
    ]
    light_id = registry.async_get_entity_id("light", DOMAIN, f"{ADDRESS}:light")
    assert (
        registry.async_get(switch_id).device_id
        == registry.async_get(light_id).device_id
    )
    mock_client.async_control.assert_not_awaited()


async def test_enable_switch_publishes_device_readback_not_requested_values(
    hass, config_entry, mock_client, program
):
    coordinator = await setup_programs(hass, config_entry, mock_client, (program,))
    confirmed = replace(program, flags=0x80)
    mock_client.async_set_program_enabled.return_value = confirmed
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": entity_id(hass, "program:1")}, blocking=True
    )
    mock_client.async_set_program_enabled.assert_awaited_once_with(
        1, False, expected=program
    )
    assert coordinator.data.get(1) == confirmed
    assert hass.states.get(entity_id(hass, "program:1")).state == "off"
    mock_client.async_control.assert_not_awaited()


async def test_stale_program_switch_reports_conflict_without_publishing_requested_state(
    hass, config_entry, mock_client, program
):
    coordinator = await setup_programs(hass, config_entry, mock_client, (program,))
    mock_client.async_set_program_enabled.side_effect = ProgramConflictError(
        "changed in app"
    )
    with pytest.raises(HomeAssistantError, match="refresh programs"):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": entity_id(hass, "program:1")},
            blocking=True,
        )
    mock_client.async_set_program_enabled.assert_awaited_once_with(
        1, False, expected=program
    )
    assert coordinator.data.get(1) == program
    assert not coordinator.last_update_success
    assert config_entry.runtime_data.last_update_success


async def test_refresh_adds_program_and_confirmed_deletion_removes_only_its_switch(
    hass, config_entry, mock_client, program
):
    coordinator = await setup_programs(hass, config_entry, mock_client, (program,))
    first_id = entity_id(hass, "program:1")
    second = replace(program, slot=2, name="Morning")
    mock_client.async_get_programs.return_value = ProgramSnapshot(
        programs=(program, second)
    )
    await coordinator.async_refresh_programs()
    await hass.async_block_till_done()
    second_id = entity_id(hass, "program:2")
    assert hass.states.get(second_id) is not None
    await coordinator.async_delete_program(1, expected=program)
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get(first_id) is None
    assert hass.states.get(first_id) is None
    assert hass.states.get(second_id).state == "on"
    mock_client.async_delete_program.assert_awaited_once_with(1, expected=program)


async def test_queued_switch_keeps_baseline_when_refresh_discovers_replacement(
    hass, config_entry, mock_client, program
):
    coordinator = await setup_programs(hass, config_entry, mock_client, (program,))
    replacement = replace(program, name="App replacement")
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked_refresh():
        started.set()
        await release.wait()
        return ProgramSnapshot(programs=(replacement,))

    async def checked_toggle(slot, enabled, *, expected):
        if expected != replacement:
            raise ProgramConflictError("changed in app")
        return replace(replacement, flags=0x80)

    mock_client.async_get_programs.side_effect = blocked_refresh
    mock_client.async_set_program_enabled.side_effect = checked_toggle
    refresh = asyncio.create_task(coordinator.async_refresh_programs())
    await asyncio.wait_for(started.wait(), 1)
    toggle = asyncio.create_task(coordinator.async_set_program_enabled(1, False))
    await asyncio.sleep(0)  # The request reaches the lock held by the refresh.
    release.set()
    await refresh
    with pytest.raises(ProgramConflictError):
        await toggle
    mock_client.async_set_program_enabled.assert_awaited_once_with(
        1, False, expected=program
    )
    assert coordinator.data.get(1) == replacement
    assert not coordinator.last_update_success


async def test_failed_refresh_keeps_slots_and_refresh_button_allows_recovery(
    hass, config_entry, mock_client, program
):
    coordinator = await setup_programs(hass, config_entry, mock_client, (program,))
    switch_id = entity_id(hass, "program:1")
    refresh_id = entity_id(hass, "refresh_programs")
    mock_client.async_get_programs.side_effect = HatchRestError("RX timed out")
    with pytest.raises(HomeAssistantError, match="Could not read"):
        await coordinator.async_refresh_programs()
    await hass.async_block_till_done()
    assert hass.states.get(switch_id).state == "unavailable"
    assert er.async_get(hass).async_get(switch_id) is not None
    assert hass.states.get(refresh_id).state != "unavailable"
    assert config_entry.runtime_data.last_update_success
    mock_client.async_get_programs.side_effect = None
    await hass.services.async_call(
        "button", "press", {"entity_id": refresh_id}, blocking=True
    )
    assert hass.states.get(switch_id).state == "on"


async def test_failed_or_conflicting_save_never_publishes_draft(
    hass, config_entry, mock_client, program
):
    coordinator = await setup_programs(hass, config_entry, mock_client, (program,))
    draft = replace(program, name="Draft")
    mock_client.async_save_program.side_effect = ProgramConflictError("changed in app")
    with pytest.raises(ProgramConflictError):
        await coordinator.async_save_program(draft, expected=program)
    assert coordinator.data.get(1) == program
    assert not coordinator.last_update_success
    assert config_entry.runtime_data.last_update_success


async def test_initial_program_failure_preserves_registered_slots_until_complete_read(
    hass, config_entry, mock_client
):
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "switch", DOMAIN, f"{ADDRESS}:program:1", config_entry=config_entry
    )
    mock_client.async_get_programs.side_effect = HatchRestError("no program RX")
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registry.async_get(old.entity_id) is not None
    coordinator = config_entry.runtime_data.program_coordinator
    mock_client.async_get_programs.side_effect = None
    await coordinator.async_refresh_programs()
    await hass.async_block_till_done()
    assert registry.async_get(old.entity_id) is None


async def test_shutdown_cancels_program_request_before_closing_shared_client(
    hass, config_entry, mock_client, program
):
    coordinator = await setup_programs(hass, config_entry, mock_client, (program,))
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_read():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    mock_client.async_get_programs.side_effect = blocked_read
    refresh = asyncio.create_task(coordinator.async_refresh_programs())
    await asyncio.wait_for(started.wait(), 1)
    await config_entry.runtime_data.async_shutdown()
    await asyncio.gather(refresh, return_exceptions=True)
    assert cancelled.is_set()
    mock_client.async_close.assert_awaited_once()


async def test_initial_program_read_does_not_block_home_assistant_startup(
    hass, config_entry, mock_client
):
    """An awaited API child must stay in HA's background-task bucket too."""
    started, release = asyncio.Event(), asyncio.Event()

    async def blocked_read():
        started.set()
        await release.wait()
        return ProgramSnapshot(programs=())

    mock_client.async_get_programs.side_effect = blocked_read
    config_entry.add_to_hass(hass)
    try:
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(hass.async_block_till_done(), 2)
        assert not release.is_set()
        assert config_entry.runtime_data.data is not None
        assert entity_id(hass, "refresh_programs") is not None
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
