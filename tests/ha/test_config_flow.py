"""Exercise config flows through the real Home Assistant flow manager."""

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.hatch_rest_ble.config_flow import CONF_MANUAL_ADDRESS
from custom_components.hatch_rest_ble.const import (
    CONF_LINGER_SECONDS,
    CONF_POLL_INTERVAL,
    DOMAIN,
    MANUFACTURER_ID,
    MANUFACTURER_IDS,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_SECONDS,
)
from custom_components.hatch_rest_ble.protocol import (
    HatchRestError,
    UnsupportedDeviceError,
)

from .conftest import ADDRESS, NAME, make_service_info

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("source", ["user", "bluetooth"])
@pytest.mark.parametrize("marker", [b"R", b"r"])
@pytest.mark.parametrize("manufacturer_id", MANUFACTURER_IDS)
async def test_creates_entry_only_after_confirmation(
    hass, source, marker, manufacturer_id, mock_client, mock_flow_setup
):
    """Both flow sources use the same complete ID, with no eager connection."""
    service_info = make_service_info(manufacturer_data={manufacturer_id: marker})
    with patch(
        "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
        return_value=[service_info],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": source},
            data=service_info if source == "bluetooth" else None,
        )
        mock_client.async_update.assert_not_awaited()
        if source == "user":
            assert result["step_id"] == "user"
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_ADDRESS: ADDRESS}
            )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "confirm"
        mock_client.async_update.assert_not_awaited()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == ADDRESS
    assert result["title"] == NAME
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_NAME: NAME}
    mock_client.async_update.assert_awaited_once()
    mock_client.async_close.assert_awaited_once()


@pytest.mark.parametrize("source", ["user", "bluetooth"])
async def test_duplicate_id(hass, source, service_info, config_entry, mock_client):
    """An entry added during a manual selection is still caught as duplicate."""
    if source == "bluetooth":
        config_entry.add_to_hass(hass)
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_BLUETOOTH},
            data=service_info,
        )
    else:
        with patch(
            "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
            return_value=[service_info],
        ):
            result = await hass.config_entries.flow.async_init(
                DOMAIN, context={"source": config_entries.SOURCE_USER}
            )
        config_entry.add_to_hass(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: ADDRESS}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    mock_client.async_update.assert_not_awaited()


async def test_manual_flow_rejects_discovery_already_in_progress(
    hass, service_info, mock_client
):
    """Manual and discovered flows cannot configure the same Rest concurrently."""
    discovered = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=service_info,
    )
    assert discovered["step_id"] == "confirm"
    with patch(
        "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
        return_value=[service_info],
    ):
        manual = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    result = await hass.config_entries.flow.async_configure(
        manual["flow_id"], {CONF_ADDRESS: ADDRESS}
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"
    mock_client.async_update.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (HatchRestError("out of range"), "cannot_connect"),
        (UnsupportedDeviceError("wrong GATT protocol"), "unsupported_device"),
        pytest.param(
            RuntimeError("unexpected failure"),
            "unknown",
            marks=pytest.mark.no_fail_on_log_exception,
        ),
    ],
)
async def test_verification_failure_always_closes_client(
    hass, service_info, mock_client, error, reason
):
    """Retryable failures remain in the form; unsupported models abort."""
    mock_client.async_update.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=service_info,
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    if reason == "unsupported_device":
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == reason
    else:
        assert result["type"] == FlowResultType.FORM
        assert result["errors"] == {"base": reason}
    assert not hass.config_entries.async_entries(DOMAIN)
    mock_client.async_close.assert_awaited_once()


async def test_user_excludes_existing_devices(hass, service_info, config_entry):
    """An already-configured Rest never reappears in the manual chooser."""
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
        return_value=[service_info],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert {key.schema for key in result["data_schema"].schema} == {CONF_MANUAL_ADDRESS}


@pytest.mark.parametrize(
    "manual_address",
    ["aa:bb:cc:dd:ee:ff", "aa-bb-cc-dd-ee-ff", "aabbccddeeff", " AA:BB:CC:DD:EE:FF "],
)
async def test_manual_address_verifies_without_matching_advertisement(
    hass, manual_address, mock_client, mock_flow_setup
):
    """A manual MAC bypasses advertisement filtering, then verifies GATT."""
    with (
        patch(
            "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
            return_value=[],
        ),
        patch(
            "custom_components.hatch_rest_ble.config_flow.HatchRestClient",
            return_value=mock_client,
        ) as client_factory,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "user"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MANUAL_ADDRESS: manual_address}
        )
        assert result["step_id"] == "confirm"
        client_factory.assert_not_called()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == ADDRESS
    assert result["data"] == {
        CONF_ADDRESS: ADDRESS,
        CONF_NAME: "Hatch Rest EE:FF",
    }
    client_factory.assert_called_once_with(hass, ADDRESS, "Hatch Rest EE:FF")
    mock_client.async_update.assert_awaited_once()
    mock_client.async_close.assert_awaited_once()


async def test_manual_address_available_with_other_discovered_device(
    hass, service_info, mock_client, mock_flow_setup
):
    """Finding one Rest does not prevent manually configuring another."""
    second_address = "AA:BB:CC:DD:EE:01"
    with patch(
        "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
        return_value=[service_info],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        assert {key.schema for key in result["data_schema"].schema} == {
            CONF_ADDRESS,
            CONF_MANUAL_ADDRESS,
        }
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MANUAL_ADDRESS: second_address}
        )
        assert result["step_id"] == "confirm"
        mock_client.async_update.assert_not_awaited()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        await hass.async_block_till_done()
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == second_address


@pytest.mark.parametrize(
    "manual_address",
    ["Nursery Rest", "AA:BB:CC", "AA:BB-CC:DD:EE:FF", "AA:BB:CC:DD:EE:FG"],
)
async def test_invalid_manual_address(hass, manual_address, mock_client):
    """Invalid MAC strings stay in the form without touching Bluetooth."""
    with patch(
        "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MANUAL_ADDRESS: manual_address}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {CONF_MANUAL_ADDRESS: "invalid_address"}
    mock_client.async_update.assert_not_awaited()


@pytest.mark.parametrize(
    "user_input",
    [{}, {CONF_ADDRESS: ADDRESS, CONF_MANUAL_ADDRESS: "AA:BB:CC:DD:EE:01"}],
)
async def test_user_requires_one_device(hass, service_info, user_input, mock_client):
    """Require exactly one selection, including when a chooser is present."""
    with patch(
        "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
        return_value=[service_info],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input
        )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {"base": "select_one_device"}
    mock_client.async_update.assert_not_awaited()


async def test_manual_address_rejects_existing_entry(hass, config_entry, mock_client):
    """A different MAC input format must still identify an existing entry."""
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MANUAL_ADDRESS: "aabbccddeeff"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    mock_client.async_update.assert_not_awaited()


async def test_manual_address_rejects_discovery_in_progress(
    hass, service_info, mock_client
):
    """Manual input and Bluetooth discovery use the same flow identity."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=service_info,
    )
    assert result["step_id"] == "confirm"
    with patch(
        "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
        return_value=[],
    ):
        manual = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            manual["flow_id"], {CONF_MANUAL_ADDRESS: "aa-bb-cc-dd-ee-ff"}
        )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_in_progress"
    mock_client.async_update.assert_not_awaited()


async def test_manual_address_requires_connectable_ha_route(hass, mock_client):
    """A valid MAC still reports the API's missing-connectable-route failure."""
    mock_client.async_update.side_effect = HatchRestError("No connectable HA route")
    with patch(
        "custom_components.hatch_rest_ble.config_flow.bluetooth.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_MANUAL_ADDRESS: ADDRESS}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": "cannot_connect"}
    assert not hass.config_entries.async_entries(DOMAIN)
    mock_client.async_close.assert_awaited_once()


@pytest.mark.parametrize(
    "change",
    ["manufacturer", "connectable", "empty", "other_model", "RestDFU", "NOSDDFU"],
)
async def test_rejects_unrelated_or_nonconnectable_discovery(hass, change):
    """Hatch commands require a connectable route and Hatch advertisement."""
    if change == "manufacturer":
        service_info = make_service_info(manufacturer_data={9999: b"R"})
    elif change == "connectable":
        service_info = make_service_info(connectable=False)
    elif change in {"RestDFU", "NOSDDFU"}:
        service_info = make_service_info(name=change)
    else:
        payload = b"" if change == "empty" else b"X"
        service_info = make_service_info(manufacturer_data={MANUFACTURER_ID: payload})
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_BLUETOOTH},
        data=service_info,
    )
    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "unsupported_device"


async def _open_polling(hass, entry_id):
    """Navigate the native options menu without reading any programs."""
    result = await hass.config_entries.options.async_init(entry_id)
    assert result["type"] == FlowResultType.MENU
    assert set(result["menu_options"]) == {"programs", "polling"}
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "polling"}
    )


@pytest.mark.parametrize("saved_interval", [None, 42])
async def test_options_show_current_or_default_poll_interval(
    hass, config_entry, mock_client, saved_interval
):
    """Opening options reads entry preferences without connecting to the Rest."""
    config_entry.add_to_hass(hass)
    if saved_interval is not None:
        hass.config_entries.async_update_entry(
            config_entry, options={CONF_POLL_INTERVAL: saved_interval}
        )
    result = await _open_polling(hass, config_entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "polling"
    expected = saved_interval if saved_interval is not None else POLL_INTERVAL_SECONDS
    assert result["data_schema"]({}) == {
        CONF_POLL_INTERVAL: expected,
        CONF_LINGER_SECONDS: 0,
    }
    mock_client.async_update.assert_not_awaited()
    mock_client.async_control.assert_not_awaited()
    mock_client.async_close.assert_not_awaited()


@pytest.mark.parametrize(
    "interval", [MIN_POLL_INTERVAL_SECONDS, 15.0, "20", MAX_POLL_INTERVAL_SECONDS]
)
async def test_options_store_interval_and_request_reload(
    hass, config_entry, mock_client, interval
):
    """Changed options trigger HA's reload lifecycle without direct BLE calls."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={"future_option": "preserved"}
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await _open_polling(hass, config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: interval}
        )
    expected = {"future_option": "preserved", CONF_POLL_INTERVAL: int(interval)}
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"] == expected
    assert config_entry.options == expected
    reload_entry.assert_called_once_with(config_entry.entry_id)
    mock_client.async_update.assert_not_awaited()
    mock_client.async_control.assert_not_awaited()
    mock_client.async_close.assert_not_awaited()


@pytest.mark.parametrize("interval", [14, 301, True, "invalid", None])
async def test_options_schema_rejects_invalid_poll_interval(
    hass, config_entry, mock_client, interval
):
    """HA applies the number selector's type and bounds before the flow step."""
    config_entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await _open_polling(hass, config_entry.entry_id)
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], {CONF_POLL_INTERVAL: interval}
            )
    assert config_entry.options == {}
    reload_entry.assert_not_called()
    mock_client.async_update.assert_not_awaited()
    mock_client.async_control.assert_not_awaited()
    mock_client.async_close.assert_not_awaited()


async def test_options_reject_fractional_poll_interval(hass, config_entry, mock_client):
    """The flow enforces whole seconds because number selectors do not round."""
    config_entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await _open_polling(hass, config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 15.5}
        )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "polling"
    assert result["errors"] == {CONF_POLL_INTERVAL: "invalid_poll_interval"}
    assert config_entry.options == {}
    reload_entry.assert_not_called()
    mock_client.async_update.assert_not_awaited()
    mock_client.async_control.assert_not_awaited()
    mock_client.async_close.assert_not_awaited()


async def test_options_unchanged_do_not_request_reload(hass, config_entry, mock_client):
    """Saving the current interval avoids unnecessary entry teardown and setup."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_POLL_INTERVAL: 30}
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await _open_polling(hass, config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 30}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_POLL_INTERVAL: 30}
    reload_entry.assert_not_called()
    mock_client.async_update.assert_not_awaited()
    mock_client.async_close.assert_not_awaited()


async def test_options_saved_poll_interval_below_new_minimum_is_clamped(
    hass, config_entry, mock_client
):
    """Entries saved by 0.2.x at 5-14 seconds open with the 15-second minimum."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_POLL_INTERVAL: 5}
    )
    result = await _open_polling(hass, config_entry.entry_id)
    assert result["data_schema"]({})[CONF_POLL_INTERVAL] == MIN_POLL_INTERVAL_SECONDS


@pytest.mark.parametrize("linger", [0, 30, 120])
async def test_options_store_linger(hass, config_entry, mock_client, linger):
    """A chosen hold is stored with the interval; HA reloads to apply it."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={CONF_LINGER_SECONDS: 10}
    )
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await _open_polling(hass, config_entry.entry_id)
        assert result["data_schema"]({})[CONF_LINGER_SECONDS] == 10
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_POLL_INTERVAL: 60, CONF_LINGER_SECONDS: linger}
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert config_entry.options == {
        CONF_POLL_INTERVAL: 60,
        CONF_LINGER_SECONDS: linger,
    }


async def test_options_reject_fractional_linger(hass, config_entry, mock_client):
    config_entry.add_to_hass(hass)
    result = await _open_polling(hass, config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 60, CONF_LINGER_SECONDS: 2.5}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_LINGER_SECONDS: "invalid_linger"}
    assert config_entry.options == {}
