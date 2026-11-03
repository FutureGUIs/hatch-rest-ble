"""Native program forms through HA's real options flow manager."""

import math
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError

from custom_components.hatch_rest_ble.const import CONF_POLL_INTERVAL
from custom_components.hatch_rest_ble.programs import (
    ProgramConflictError,
    ProgramSnapshot,
    RestProgram,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def native_program():
    """Keep opaque bytes and non-minute/raw-percent values in the baseline."""
    return RestProgram(
        slot=2,
        name="Bedtime",
        start=datetime(2026, 10, 15, 20, 0, 30),
        duration=12 * 3600 + 30,
        days=0x7F,
        color=(10, 20, 30),
        brightness=137,
        sound=99,
        volume=140,
        flags=0xED,
        buttons=0x1200,
        power=0xA5,
        presets=0xDEADBEEF,
    )


@pytest.fixture
async def program_manager(hass, config_entry, mock_client, native_program):
    """Load a real entry while mocking only device I/O and platform setup."""
    manager = MagicMock()
    manager.data = ProgramSnapshot(
        (native_program,),
        device_time=datetime(2026, 11, 1, 1, 10),
        firmware_version="1.0.4.31",
        volume_min=8,
    )
    manager.async_refresh = AsyncMock()
    manager.async_refresh_programs = AsyncMock()
    manager.async_shutdown = AsyncMock()

    async def save(program, *, expected):
        manager.data = replace(
            manager.data,
            programs=tuple(
                item for item in manager.data.programs if item.slot != program.slot
            )
            + (program,),
        )

    async def delete(slot, *, expected):
        manager.data = replace(
            manager.data,
            programs=tuple(item for item in manager.data.programs if item.slot != slot),
        )

    async def enabled(slot, enabled, *, expected):
        program = manager.data.get(slot)
        await save(
            replace(program, flags=(program.flags & ~0x40) | (0x40 if enabled else 0)),
            expected=program,
        )

    manager.save_impl = save
    manager.async_save_program = AsyncMock(side_effect=save)
    manager.async_delete_program = AsyncMock(side_effect=delete)
    manager.async_set_program_enabled = AsyncMock(side_effect=enabled)
    config_entry.add_to_hass(hass)
    with (
        patch(
            "custom_components.hatch_rest_ble.HatchRestProgramCoordinator",
            return_value=manager,
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ),
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert config_entry.state == ConfigEntryState.LOADED
        yield manager
        if config_entry.state == ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(config_entry.entry_id)
            await hass.async_block_till_done()


async def _programs(hass, config_entry):
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "programs"}
    )
    assert result["step_id"] == "programs"
    return result


async def _select(hass, result, choice="2", action="edit_program"):
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"choice": choice}
    )
    if choice != "add":
        assert result["step_id"] == "program_actions"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": action}
        )
    return result


async def test_program_list_reads_once_and_explicit_refresh_reads_again(
    hass, config_entry, program_manager
):
    result = await _programs(hass, config_entry)
    program_manager.async_refresh_programs.assert_awaited_once()
    assert "(+1 day)" in result["description_placeholders"]["programs"]
    assert "2026-11-01 01:10:00" in result["description_placeholders"]["device_clock"]
    editor = await _select(hass, result)
    assert editor["step_id"] == "edit_program"
    program_manager.async_refresh_programs.assert_awaited_once()
    # Explicit editor reload is a device read, rather than a write or HA reload.
    await hass.config_entries.options.async_configure(
        editor["flow_id"], {**editor["data_schema"]({}), "refresh": True}
    )
    assert program_manager.async_refresh_programs.await_count == 2
    program_manager.async_save_program.assert_not_awaited()


async def test_name_edit_preserves_seconds_raw_values_and_opaque_fields(
    hass, config_entry, program_manager, native_program
):
    result = await _select(hass, await _programs(hass, config_entry))
    values = {**result["data_schema"]({}), "name": "Quiet bedtime"}
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], values
        )
    assert result["step_id"] == "programs"
    program_manager.async_save_program.assert_awaited_once_with(
        replace(native_program, name="Quiet bedtime"), expected=native_program
    )
    assert config_entry.options == {}
    reload_entry.assert_not_called()


async def test_new_program_uses_empty_slot_and_device_local_date(
    hass, config_entry, program_manager
):
    result = await _select(hass, await _programs(hass, config_entry), choice="add")
    values = {
        **result["data_schema"]({}),
        "name": "Morning",
        "weekdays": ["mon", "sun"],
        "start_time": "01:30:00",
        "brightness": 40,
        "volume": 25,
    }
    await hass.config_entries.options.async_configure(result["flow_id"], values)
    call = program_manager.async_save_program.await_args
    program = call.args[0]
    assert program.slot == 1
    assert program.start == datetime(2026, 11, 1, 1, 30)
    assert program.start.tzinfo is None
    assert program.days == 0x03  # Native Sunday bit0 / Monday bit1.
    assert program.brightness == 102
    assert program.volume == math.floor(25 * 247 / 100 + 0.5) + 8
    assert program.flags == 0xC0
    assert call.kwargs == {"expected": None}


async def test_new_program_without_device_clock_uses_ha_local_date(
    hass, config_entry, program_manager
):
    program_manager.data = replace(program_manager.data, device_time=None)
    result = await _programs(hass, config_entry)
    local_now = datetime(2026, 10, 15, 23, 30, tzinfo=ZoneInfo("America/New_York"))
    with patch(
        "custom_components.hatch_rest_ble.config_flow.dt_util.now",
        return_value=local_now,
    ):
        result = await _select(hass, result, choice="add")
    await hass.config_entries.options.async_configure(
        result["flow_id"], result["data_schema"]({})
    )
    program = program_manager.async_save_program.await_args.args[0]
    assert program.start == datetime(2026, 10, 15, 20)
    assert program.start.tzinfo is None


async def test_absent_marker_does_not_reserve_an_empty_slot(
    hass, config_entry, program_manager, native_program
):
    absent = replace(native_program, slot=1, flags=0, name="", duration=0)
    program_manager.data = replace(
        program_manager.data, programs=(absent, native_program)
    )
    result = await _select(hass, await _programs(hass, config_entry), choice="add")
    await hass.config_entries.options.async_configure(
        result["flow_id"], result["data_schema"]({})
    )
    assert program_manager.async_save_program.await_args.args[0].slot == 1
    assert program_manager.async_save_program.await_args.kwargs == {"expected": None}


async def test_full_table_does_not_offer_new_program(
    hass, config_entry, program_manager, native_program
):
    program_manager.data = replace(
        program_manager.data,
        programs=tuple(replace(native_program, slot=slot) for slot in range(1, 11)),
    )
    result = await _programs(hass, config_entry)
    selection = next(iter(result["data_schema"].schema.values()))
    options = {option["value"] for option in selection.config["options"]}
    assert options == {*(str(slot) for slot in range(1, 11)), "refresh"}
    program_manager.async_save_program.assert_not_awaited()


async def test_toddler_lock_updates_mask_preserving_unknown_button_bits(
    hass, config_entry, program_manager, native_program
):
    result = await _select(hass, await _programs(hass, config_entry))
    values = {**result["data_schema"]({}), "toddler_lock": True, "enabled": False}
    await hass.config_entries.options.async_configure(result["flow_id"], values)
    program = program_manager.async_save_program.await_args.args[0]
    assert program.buttons == native_program.buttons | 0x01FF
    assert program.flags == native_program.flags & ~0x40
    assert program.presets == native_program.presets
    assert program.power == native_program.power


async def test_old_firmware_omits_lock_and_preserves_original_buttons(
    hass, config_entry, program_manager, native_program
):
    program_manager.data = replace(program_manager.data, firmware_version="1.0.4.30")
    result = await _select(hass, await _programs(hass, config_entry))
    assert "toddler_lock" not in {key.schema for key in result["data_schema"].schema}
    values = {**result["data_schema"]({}), "name": "Earlier firmware"}
    await hass.config_entries.options.async_configure(result["flow_id"], values)
    assert (
        program_manager.async_save_program.await_args.args[0].buttons
        == native_program.buttons
    )


@pytest.mark.parametrize("name", ["", "   ", "A" * 17, "Café", "OK", "E01", "Unknown"])
async def test_invalid_program_names_do_not_write(
    hass, config_entry, program_manager, name
):
    result = await _select(hass, await _programs(hass, config_entry))
    values = {**result["data_schema"]({}), "name": name}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], values
    )
    assert result["errors"] == {"name": "invalid_program_name"}
    assert result["step_id"] == "edit_program"
    assert result["data_schema"]({})["name"] == name
    program_manager.async_save_program.assert_not_awaited()


async def test_zero_duration_does_not_write(hass, config_entry, program_manager):
    result = await _select(hass, await _programs(hass, config_entry))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**result["data_schema"]({}), "duration_hours": 0, "duration_minutes": 0},
    )
    assert result["errors"] == {"duration_minutes": "invalid_program_duration"}
    program_manager.async_save_program.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("duration_hours", 12.5),
        ("duration_minutes", 1.5),
        ("brightness", 50.5),
        ("volume", 20.5),
    ],
)
async def test_fractional_editor_values_do_not_write(
    hass, config_entry, program_manager, field, value
):
    result = await _select(hass, await _programs(hass, config_entry))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**result["data_schema"]({}), field: value}
    )
    assert result["errors"] == {field: "whole_number_required"}
    program_manager.async_save_program.assert_not_awaited()


async def test_save_conflict_keeps_draft_until_explicit_reload(
    hass, config_entry, program_manager, native_program
):
    result = await _select(hass, await _programs(hass, config_entry))
    program_manager.async_save_program.side_effect = ProgramConflictError(
        "changed in app"
    )
    values = {**result["data_schema"]({}), "name": "My edit"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], values
    )
    assert result["errors"] == {"base": "program_conflict"}
    assert result["data_schema"]({})["name"] == "My edit"
    assert (
        program_manager.async_save_program.await_args.kwargs["expected"]
        == native_program
    )
    app_version = replace(native_program, name="Changed in app", duration=7200)
    program_manager.data = replace(program_manager.data, programs=(app_version,))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**result["data_schema"]({}), "refresh": True}
    )
    assert result["data_schema"]({})["name"] == "Changed in app"
    program_manager.async_save_program.side_effect = program_manager.save_impl
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**result["data_schema"]({}), "name": "Reviewed edit"}
    )
    assert (
        program_manager.async_save_program.await_args.kwargs["expected"] == app_version
    )


async def test_new_slot_conflict_never_becomes_an_existing_slot_overwrite(
    hass, config_entry, program_manager
):
    result = await _select(hass, await _programs(hass, config_entry), choice="add")
    program_manager.async_save_program.side_effect = ProgramConflictError(
        "slot occupied"
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], result["data_schema"]({})
    )
    assert result["errors"] == {"base": "program_conflict"}
    assert program_manager.async_save_program.await_args.kwargs == {"expected": None}


async def test_delete_requires_confirmation_and_keeps_expected_on_conflict(
    hass, config_entry, program_manager, native_program
):
    result = await _select(
        hass, await _programs(hass, config_entry), action="delete_program"
    )
    assert "Bedtime" in result["description_placeholders"]["program"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": False}
    )
    assert result["errors"] == {"confirm": "confirmation_required"}
    program_manager.async_delete_program.assert_not_awaited()
    program_manager.async_delete_program.side_effect = ProgramConflictError("changed")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["errors"] == {"base": "program_conflict"}
    program_manager.async_delete_program.assert_awaited_once_with(
        2, expected=native_program
    )


async def test_delete_refresh_shows_new_summary_before_deleting(
    hass, config_entry, program_manager, native_program
):
    result = await _select(
        hass, await _programs(hass, config_entry), action="delete_program"
    )
    app_version = replace(native_program, name="App version")
    program_manager.data = replace(program_manager.data, programs=(app_version,))
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True, "refresh": True}
    )
    assert "App version" in result["description_placeholders"]["program"]
    program_manager.async_delete_program.assert_not_awaited()
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    program_manager.async_delete_program.assert_awaited_once_with(
        2, expected=app_version
    )
    assert result["step_id"] == "programs"


async def test_enable_toggle_uses_shared_coordinator_without_storing_options(
    hass, config_entry, program_manager, native_program
):
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_POLL_INTERVAL: 30, "future_option": "keep"}
    )
    result = await _select(
        hass, await _programs(hass, config_entry), action="toggle_program"
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"confirm": True}
        )
    assert result["step_id"] == "programs"
    program_manager.async_set_program_enabled.assert_awaited_once_with(
        2, False, expected=native_program
    )
    assert config_entry.options == {CONF_POLL_INTERVAL: 30, "future_option": "keep"}
    reload_entry.assert_not_called()


async def test_enable_confirmation_preserves_baseline_after_app_replaces_slot(
    hass, config_entry, program_manager, native_program
):
    result = await _select(
        hass, await _programs(hass, config_entry), action="toggle_program"
    )
    replacement = replace(native_program, name="App replacement")
    program_manager.data = replace(program_manager.data, programs=(replacement,))
    program_manager.async_set_program_enabled.side_effect = ProgramConflictError(
        "changed in app"
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"confirm": True}
    )
    assert result["errors"] == {"base": "program_conflict"}
    program_manager.async_set_program_enabled.assert_awaited_once_with(
        2, False, expected=native_program
    )
    assert program_manager.data.get(2) == replacement


async def test_read_error_keeps_refresh_form(hass, config_entry, program_manager):
    program_manager.async_refresh_programs.side_effect = HomeAssistantError(
        "out of range"
    )
    result = await _programs(hass, config_entry)
    assert result["errors"] == {"base": "program_read_failed"}
    program_manager.async_refresh_programs.side_effect = None
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"choice": "refresh"}
    )
    assert not result["errors"]
    assert "Bedtime" in result["description_placeholders"]["programs"]


async def test_write_error_keeps_editor_values(hass, config_entry, program_manager):
    result = await _select(hass, await _programs(hass, config_entry))
    program_manager.async_save_program.side_effect = HomeAssistantError("not confirmed")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**result["data_schema"]({}), "name": "Keep this edit"}
    )
    assert result["step_id"] == "edit_program"
    assert result["errors"] == {"base": "program_write_failed"}
    assert result["data_schema"]({})["name"] == "Keep this edit"


async def test_failed_editor_reload_keeps_unsaved_input(
    hass, config_entry, program_manager
):
    result = await _select(hass, await _programs(hass, config_entry))
    program_manager.async_refresh_programs.side_effect = HomeAssistantError(
        "out of range"
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**result["data_schema"]({}), "name": "Pending edit", "refresh": True},
    )
    assert result["errors"] == {"base": "program_read_failed"}
    assert result["data_schema"]({})["name"] == "Pending edit"
    program_manager.async_save_program.assert_not_awaited()


async def test_unloaded_entry_program_editor_aborts_gracefully(hass, config_entry):
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "programs"}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "device_not_loaded"
