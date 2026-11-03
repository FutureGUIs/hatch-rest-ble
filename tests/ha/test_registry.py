"""Remove only the retired Power switch after a successful integration setup."""

from unittest.mock import Mock

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hatch_rest_ble.const import DOMAIN
from custom_components.hatch_rest_ble.protocol import HatchRestError

from .conftest import ADDRESS

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("disabled_by", [None, er.RegistryEntryDisabler.USER])
async def test_setup_removes_renamed_legacy_power_switch(
    hass, config_entry, mock_client, rest_state, disabled_by
):
    """Find the retired switch by its stable identity, even renamed or disabled."""
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"{ADDRESS}:power",
        config_entry=config_entry,
        suggested_object_id="nursery_rest_power",
        disabled_by=disabled_by,
    )
    legacy = registry.async_update_entity(
        legacy.entity_id, new_entity_id="switch.renamed_bedtime_control"
    )
    initial_read = Mock()

    async def read_before_migration():
        assert registry.async_get(legacy.entity_id) is not None
        initial_read()
        return rest_state

    mock_client.async_update.side_effect = read_before_migration
    try:
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        initial_read.assert_called_once_with()
        assert registry.async_get(legacy.entity_id) is None
        assert (
            registry.async_get_entity_id("switch", DOMAIN, f"{ADDRESS}:power") is None
        )
        assert hass.states.get(legacy.entity_id) is None
        assert not any(
            entry.domain == "switch"
            for entry in er.async_entries_for_config_entry(
                registry, config_entry.entry_id
            )
        )
    finally:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


async def test_migration_preserves_other_integrations_and_other_rests(
    hass, config_entry, mock_client
):
    """Removing one Rest's old switch must not remove any unrelated entity."""
    config_entry.add_to_hass(hass)
    # Disabled so domain setup does not load it; a loaded entry would
    # correctly remove its own legacy switch and mask what this test checks.
    other_rest = MockConfigEntry(
        domain=DOMAIN,
        title="Other Rest",
        unique_id="AA:BB:CC:DD:EE:01",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:01", CONF_NAME: "Other Rest"},
        disabled_by=ConfigEntryDisabler.USER,
    )
    other_rest.add_to_hass(hass)
    other_integration = MockConfigEntry(domain="unrelated_integration")
    other_integration.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "switch", DOMAIN, f"{ADDRESS}:power", config_entry=config_entry
    )
    other_rest_switch = registry.async_get_or_create(
        "switch",
        DOMAIN,
        "AA:BB:CC:DD:EE:01:power",
        config_entry=other_rest,
    )
    unrelated_switch = registry.async_get_or_create(
        "switch",
        "unrelated_integration",
        f"{ADDRESS}:power",
        config_entry=other_integration,
    )
    try:
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(legacy.entity_id) is None
        assert registry.async_get(other_rest_switch.entity_id) == other_rest_switch
        assert registry.async_get(unrelated_switch.entity_id) == unrelated_switch
    finally:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


async def test_migration_checks_matching_switch_config_entry_owner(
    hass, config_entry, mock_client
):
    """Even an exact domain/platform/unique-ID match must belong to this entry."""
    config_entry.add_to_hass(hass)
    other_rest = MockConfigEntry(
        domain=DOMAIN,
        title="Other Rest",
        unique_id="AA:BB:CC:DD:EE:01",
        data={CONF_ADDRESS: "AA:BB:CC:DD:EE:01", CONF_NAME: "Other Rest"},
    )
    other_rest.add_to_hass(hass)
    registry = er.async_get(hass)
    other_owned = registry.async_get_or_create(
        "switch", DOMAIN, f"{ADDRESS}:power", config_entry=other_rest
    )
    try:
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(other_owned.entity_id) == other_owned
    finally:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


async def test_migration_preserves_existing_light_and_media_ids_on_reload(
    hass, config_entry, mock_client
):
    """Only the switch disappears; renamed surviving controls retain identity."""
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "switch", DOMAIN, f"{ADDRESS}:power", config_entry=config_entry
    )
    light = registry.async_get_or_create(
        "light",
        DOMAIN,
        f"{ADDRESS}:light",
        config_entry=config_entry,
        suggested_object_id="my_bedtime_lamp",
    )
    player = registry.async_get_or_create(
        "media_player",
        DOMAIN,
        f"{ADDRESS}:sound",
        config_entry=config_entry,
        suggested_object_id="my_sleep_sounds",
    )
    try:
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
        assert registry.async_get(legacy.entity_id) is None
        for domain, suffix, original in (
            ("light", "light", light),
            ("media_player", "sound", player),
        ):
            assert (
                registry.async_get_entity_id(domain, DOMAIN, f"{ADDRESS}:{suffix}")
                == original.entity_id
            )
            assert registry.async_get(original.entity_id).id == original.id
            assert hass.states.get(original.entity_id) is not None
    finally:
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()


async def test_failed_first_refresh_preserves_legacy_power_switch(
    hass, config_entry, mock_client
):
    """The old switch is removed only after a successful device verification."""
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy = registry.async_get_or_create(
        "switch", DOMAIN, f"{ADDRESS}:power", config_entry=config_entry
    )
    mock_client.async_update.side_effect = HatchRestError("out of range")
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state == ConfigEntryState.SETUP_RETRY
    assert registry.async_get(legacy.entity_id) == legacy
    mock_client.async_close.assert_awaited_once()
