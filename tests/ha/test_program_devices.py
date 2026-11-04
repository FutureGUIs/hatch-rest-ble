"""One device per native program: entities, batched edits, add, and delete."""

import asyncio
from dataclasses import replace
from datetime import datetime

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.hatch_rest_ble.const import DOMAIN
from custom_components.hatch_rest_ble.programs import (
    ProgramConflictError,
    ProgramSnapshot,
    RestProgram,
)
from custom_components.hatch_rest_ble.protocol import HatchRestError

from .conftest import ADDRESS

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def fast_edits(monkeypatch):
    monkeypatch.setattr(
        "custom_components.hatch_rest_ble.program_coordinator.EDIT_DEBOUNCE_SECONDS",
        0.05,
    )


@pytest.fixture
def program():
    return RestProgram(
        slot=1,
        name="Bedtime",
        start=datetime(2026, 10, 15, 19, 30),
        duration=41430,  # 690.5 minutes: the native 30-second offset is kept.
        days=0x3E,  # Monday-Friday
        color=(255, 100, 10),
        brightness=51,
        sound=7,
        volume=64,
        flags=0xC0,
        buttons=0,
    )


async def setup(hass, config_entry, mock_client, programs, firmware="1.0.4.31"):
    mock_client.async_get_programs.return_value = ProgramSnapshot(
        programs=programs, firmware_version=firmware
    )

    async def confirm(program, *, expected):
        return program

    mock_client.async_save_program.side_effect = confirm
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return config_entry.runtime_data.program_coordinator


def eid(hass, domain, slot, key=None):
    unique_id = f"{ADDRESS}:program:{slot}" + (f":{key}" if key else "")
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


def program_device(hass, slot):
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return dr.async_get(hass).async_get_child_device_by_identifier(
        (DOMAIN, f"{ADDRESS}:program:{slot}"), entry.entry_id
    )


def hub_device(hass):
    (entry,) = hass.config_entries.async_entries(DOMAIN)
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ADDRESS), entry.entry_id
    )


async def call(hass, domain, service, entity_id, **data):
    await hass.services.async_call(
        domain, service, {"entity_id": entity_id, **data}, blocking=True
    )


async def test_each_program_is_a_device_with_its_settings(
    hass, config_entry, mock_client, program
):
    second = replace(program, slot=4, name="Nap", days=0x41, sound=99)
    await setup(hass, config_entry, mock_client, (program, second))
    devices = dr.async_get(hass)
    hub = hub_device(hass)
    program_devices = dr.async_entries_for_parent_device(devices, hub.id)
    assert sorted(device.name for device in program_devices) == [
        "Nursery Rest program 1",
        "Nursery Rest program 4",
    ]
    assert dr.async_entries_for_config_entry(devices, config_entry.entry_id) == [hub]
    entities = er.async_entries_for_device(
        er.async_get(hass), program_device(hass, 1).id
    )
    # Enabled, 7 days, button lock, name, color, start, duration,
    # brightness, volume, sound.
    assert len(entities) == 16

    state = hass.states.get
    assert state(eid(hass, "text", 1, "name")).state == "Bedtime"
    assert state(eid(hass, "text", 1, "color")).state == "#FF640A"
    assert state(eid(hass, "time", 1, "start")).state == "19:30:00"
    assert state(eid(hass, "number", 1, "duration")).state == "690.5"
    assert state(eid(hass, "number", 1, "brightness")).state == "20"
    assert state(eid(hass, "number", 1, "volume")).state == "25"
    assert state(eid(hass, "select", 1, "sound")).state == "Rain"
    assert state(eid(hass, "switch", 1, "monday")).state == "on"
    assert state(eid(hass, "switch", 1, "sunday")).state == "off"
    assert state(eid(hass, "switch", 1, "button_lock")).state == "off"
    assert state(eid(hass, "switch", 1)).state == "on"
    unknown = state(eid(hass, "select", 4, "sound"))
    assert unknown.state == "Unknown sound (99)"
    assert "Unknown sound (99)" in unknown.attributes["options"]


async def test_button_lock_only_on_supporting_firmware(
    hass, config_entry, mock_client, program
):
    await setup(hass, config_entry, mock_client, (program,), firmware="1.0.4.30")
    assert eid(hass, "switch", 1, "button_lock") is None
    assert eid(hass, "switch", 1, "monday") is not None


async def test_quick_edits_are_one_verified_write(
    hass, config_entry, mock_client, program
):
    coordinator = await setup(hass, config_entry, mock_client, (program,))
    await asyncio.gather(
        call(hass, "switch", "turn_off", eid(hass, "switch", 1, "monday")),
        call(hass, "switch", "turn_on", eid(hass, "switch", 1, "sunday")),
        call(hass, "text", "set_value", eid(hass, "text", 1, "name"), value="Night"),
        call(hass, "number", "set_value", eid(hass, "number", 1, "volume"), value=50),
        call(
            hass,
            "select",
            "select_option",
            eid(hass, "select", 1, "sound"),
            option="Ocean",
        ),
    )
    mock_client.async_save_program.assert_awaited_once()
    saved = mock_client.async_save_program.await_args.args[0]
    assert mock_client.async_save_program.await_args.kwargs["expected"] == program
    assert saved == replace(program, name="Night", days=0x3D, volume=128, sound=5)
    assert coordinator.data.get(1) == saved
    assert hass.states.get(eid(hass, "text", 1, "name")).state == "Night"
    assert hass.states.get(eid(hass, "switch", 1, "monday")).state == "off"


async def test_unchanged_display_values_keep_native_values(
    hass, config_entry, mock_client, program
):
    await setup(hass, config_entry, mock_client, (program,))
    await call(
        hass, "number", "set_value", eid(hass, "number", 1, "duration"), value=690.5
    )
    await call(
        hass, "number", "set_value", eid(hass, "number", 1, "brightness"), value=20
    )
    mock_client.async_save_program.assert_not_awaited()
    await call(
        hass, "number", "set_value", eid(hass, "number", 1, "duration"), value=60
    )
    saved = mock_client.async_save_program.await_args.args[0]
    assert saved.duration == 3600
    assert saved.brightness == 51


async def test_start_time_and_color(hass, config_entry, mock_client, program):
    await setup(hass, config_entry, mock_client, (program,))
    await call(hass, "time", "set_value", eid(hass, "time", 1, "start"), time="21:15")
    assert mock_client.async_save_program.await_args.args[0].start == datetime(
        2026, 10, 15, 21, 15
    )
    await call(hass, "text", "set_value", eid(hass, "text", 1, "color"), value="00ff80")
    assert mock_client.async_save_program.await_args.args[0].color == (0, 255, 128)
    assert hass.states.get(eid(hass, "text", 1, "color")).state == "#00FF80"


async def test_enable_toggle_uses_minimal_command(
    hass, config_entry, mock_client, program
):
    await setup(hass, config_entry, mock_client, (program,))
    mock_client.async_set_program_enabled.return_value = replace(program, flags=0x80)
    await call(hass, "switch", "turn_off", eid(hass, "switch", 1))
    mock_client.async_set_program_enabled.assert_awaited_once_with(
        1, False, expected=program
    )
    mock_client.async_save_program.assert_not_awaited()
    assert hass.states.get(eid(hass, "switch", 1)).state == "off"


async def test_invalid_name_is_rejected_before_any_write(
    hass, config_entry, mock_client, program
):
    await setup(hass, config_entry, mock_client, (program,))
    with pytest.raises(HomeAssistantError, match="reserved"):
        await call(hass, "text", "set_value", eid(hass, "text", 1, "name"), value="OK")
    mock_client.async_save_program.assert_not_awaited()


async def test_failed_write_keeps_confirmed_values(
    hass, config_entry, mock_client, program
):
    coordinator = await setup(hass, config_entry, mock_client, (program,))
    mock_client.async_save_program.side_effect = HatchRestError("RX timed out")
    with pytest.raises(HomeAssistantError, match="Could not save"):
        await call(hass, "text", "set_value", eid(hass, "text", 1, "name"), value="X")
    assert coordinator.data.get(1) == program
    await coordinator.async_refresh_programs()
    assert hass.states.get(eid(hass, "text", 1, "name")).state == "Bedtime"


async def test_add_program_creates_next_free_slot_from_live_state(
    hass, config_entry, mock_client, program, rest_state
):
    await setup(hass, config_entry, mock_client, (program,))
    add = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{ADDRESS}:add_program"
    )
    await hass.services.async_call("button", "press", {"entity_id": add}, blocking=True)
    await hass.async_block_till_done()
    created = mock_client.async_save_program.await_args.args[0]
    assert mock_client.async_save_program.await_args.kwargs["expected"] is None
    assert created.slot == 2
    assert created.name == "Program 2"
    assert (created.start.hour, created.duration, created.days) == (20, 3600, 0x7F)
    assert (created.color, created.sound) == (rest_state.color, rest_state.sound)
    assert created.enabled
    assert program_device(hass, 2) is not None
    assert hass.states.get(eid(hass, "text", 2, "name")).state == "Program 2"


async def test_add_program_reports_full_table(hass, config_entry, mock_client, program):
    programs = tuple(replace(program, slot=slot) for slot in range(1, 11))
    coordinator = await setup(hass, config_entry, mock_client, programs)
    with pytest.raises(HomeAssistantError, match="All 10 program slots"):
        await coordinator.async_add_program()
    mock_client.async_save_program.assert_not_awaited()


async def test_deleting_program_device_deletes_device_program(
    hass, config_entry, mock_client, program, hass_ws_client
):
    second = replace(program, slot=2, name="Nap")
    coordinator = await setup(hass, config_entry, mock_client, (program, second))
    assert await async_setup_component(hass, "config", {})
    device = program_device(hass, 1)
    name_id = eid(hass, "text", 1, "name")
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": device.id}
    )
    response = await client.receive_json()
    assert response["success"], response
    mock_client.async_delete_program.assert_awaited_once_with(1, expected=program)
    assert coordinator.data.get(1) is None
    assert program_device(hass, 1) is None
    assert er.async_get(hass).async_get(name_id) is None
    assert program_device(hass, 2) is not None


async def test_device_deletion_failures_keep_the_device(
    hass, config_entry, mock_client, program, hass_ws_client
):
    await setup(hass, config_entry, mock_client, (program,))
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)

    mock_client.async_delete_program.side_effect = ProgramConflictError("app edit")
    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove",
            "device_id": program_device(hass, 1).id,
        }
    )
    response = await client.receive_json()
    assert not response["success"]
    assert "changed on the Rest" in response["error"]["message"]
    await hass.async_block_till_done(wait_background_tasks=True)
    assert program_device(hass, 1) is not None

    hub = hub_device(hass)
    await client.send_json_auto_id(
        {"type": "config/device_registry/remove", "device_id": hub.id}
    )
    response = await client.receive_json()
    assert not response["success"]
    assert "integration entry" in response["error"]["message"]
    assert dr.async_get(hass).async_get(hub.id) is not None


async def test_program_deleted_in_app_removes_its_device_only_after_complete_read(
    hass, config_entry, mock_client, program
):
    coordinator = await setup(hass, config_entry, mock_client, (program,))
    mock_client.async_get_programs.side_effect = HatchRestError("RX timed out")
    with pytest.raises(HomeAssistantError):
        await coordinator.async_refresh_programs()
    assert program_device(hass, 1) is not None
    mock_client.async_get_programs.side_effect = None
    mock_client.async_get_programs.return_value = ProgramSnapshot(programs=())
    await coordinator.async_refresh_programs()
    await hass.async_block_till_done()
    assert program_device(hass, 1) is None
    assert eid(hass, "switch", 1, "monday") is None


async def test_program_devices_follow_the_rest_area_and_disabled_state(
    hass, config_entry, mock_client, program
):
    await setup(hass, config_entry, mock_client, (program,))
    devices = dr.async_get(hass)
    hub = hub_device(hass)
    devices.async_update_device(hub.id, area_id="nursery")
    child = program_device(hass, 1)
    assert dr.async_get_effective_area_id(hass, child) == "nursery"
    devices.async_update_device(hub.id, disabled_by=dr.DeviceEntryDisabler.USER)
    assert program_device(hass, 1).disabled
