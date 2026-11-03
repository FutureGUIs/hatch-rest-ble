"""Entity behavior using Home Assistant's real entity classes."""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    EFFECT_OFF,
)
from homeassistant.components.media_player import MediaPlayerState
from homeassistant.exceptions import ServiceValidationError

from custom_components.hatch_rest_ble.const import COLOR_GRADIENT, DOMAIN, SOUNDS
from custom_components.hatch_rest_ble.light import EFFECT_COLOR_LOOP, HatchRestLight
from custom_components.hatch_rest_ble.media_player import HatchRestMediaPlayer
from custom_components.hatch_rest_ble.protocol import RestState


@pytest.fixture
def coordinator():
    """Record command batches built against the freshly read device state."""
    mock = MagicMock()
    mock.address = "AA:BB:CC:DD:EE:FF"
    mock.device_name = "Nursery"
    mock.data = RestState((20, 40, 60), 128, 5, 100, True, 0)
    mock.last_update_success = True
    mock.command_batches = []
    mock.fresh_data = None
    mock.reported_data = None

    async def control(build_commands):
        fresh_state = mock.fresh_data if mock.fresh_data is not None else mock.data
        mock.command_batches.append(build_commands(fresh_state))
        mock.data = (
            mock.reported_data if mock.reported_data is not None else fresh_state
        )
        mock.fresh_data = None
        mock.reported_data = None

    mock.async_control = AsyncMock(side_effect=control)
    return mock


def test_entities_share_device_but_have_distinct_unique_ids(coordinator):
    """Every platform uses the full MAC and one shared device registry entry."""
    entities = [
        HatchRestLight(coordinator),
        HatchRestMediaPlayer(coordinator),
    ]
    assert {entity.unique_id for entity in entities} == {
        "AA:BB:CC:DD:EE:FF:light",
        "AA:BB:CC:DD:EE:FF:sound",
    }
    for entity in entities:
        assert entity.has_entity_name
        assert entity.device_info["identifiers"] == {(DOMAIN, coordinator.address)}
        assert entity.device_info["name"] == "Nursery"
        assert entity.available
    coordinator.last_update_success = False
    assert all(not entity.available for entity in entities)


def test_dark_light_and_muted_audio_report_zero(coordinator):
    coordinator.data = replace(coordinator.data, brightness=0, volume=0)
    light = HatchRestLight(coordinator)
    player = HatchRestMediaPlayer(coordinator)
    assert light.brightness == 0
    assert not light.is_on
    assert player.volume_level == 0.0
    assert player.state == MediaPlayerState.PLAYING


@pytest.mark.parametrize(
    ("power", "sound", "expected"),
    [
        (False, 0, MediaPlayerState.OFF),
        (False, 5, MediaPlayerState.OFF),
        (True, 0, MediaPlayerState.IDLE),
        (True, 5, MediaPlayerState.PLAYING),
    ],
)
def test_media_state_tracks_device_power(coordinator, power, sound, expected):
    """A silent powered device remains on so its power toggle can switch it off."""
    coordinator.data = replace(coordinator.data, power=power, sound=sound)
    assert HatchRestMediaPlayer(coordinator).state == expected


def test_second_legacy_hardware_revision_is_identified(coordinator):
    coordinator.data = replace(coordinator.data, hardware_version="2.0", volume_min=8)
    player = HatchRestMediaPlayer(coordinator)
    assert player.device_info["hw_version"] == "2.0"


@pytest.mark.parametrize(
    "volume, expected", [(0, 0), (8, 0), (255, 1), (132, 124 / 247)]
)
def test_second_legacy_revision_volume_level(coordinator, volume, expected):
    coordinator.data = replace(
        coordinator.data, hardware_version="2.0", volume_min=8, volume=volume
    )
    assert HatchRestMediaPlayer(coordinator).volume_level == pytest.approx(expected)


@pytest.mark.asyncio
async def test_light_turn_on_combines_color_and_brightness(coordinator):
    light = HatchRestLight(coordinator)
    await light.async_turn_on(**{ATTR_RGB_COLOR: (255, 10, 30), ATTR_BRIGHTNESS: 200})
    assert coordinator.command_batches == [[b"SCFF0A1EC8"]]


@pytest.mark.asyncio
async def test_light_turn_on_reads_fresh_power_before_writing(coordinator):
    light = HatchRestLight(coordinator)
    coordinator.fresh_data = replace(
        coordinator.data, color=(100, 50, 10), brightness=30, power=False
    )
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 200})
    assert coordinator.command_batches == [[b"SI01", b"SC64320AC8"]]


@pytest.mark.asyncio
async def test_zero_brightness_does_not_power_on_device(coordinator):
    coordinator.data = replace(coordinator.data, power=False)
    light = HatchRestLight(coordinator)
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 0})
    assert coordinator.command_batches == [[b"SC14283C00"]]


@pytest.mark.asyncio
async def test_light_off_preserves_fresh_color_and_sound(coordinator):
    light = HatchRestLight(coordinator)
    coordinator.fresh_data = replace(coordinator.data, color=(1, 2, 3), sound=7)
    await light.async_turn_off()
    assert coordinator.command_batches == [[b"SC01020300"]]
    coordinator.data = replace(coordinator.data, brightness=0)
    await light.async_turn_on()
    assert coordinator.command_batches[-1] == [b"SC01020380"]


@pytest.mark.asyncio
async def test_gradient_is_an_effect_and_off_restores_static_white(coordinator):
    light = HatchRestLight(coordinator)
    await light.async_turn_on(**{ATTR_EFFECT: EFFECT_COLOR_LOOP})
    assert coordinator.command_batches[-1] == [b"SCFEFEFE80"]
    coordinator.data = replace(coordinator.data, color=COLOR_GRADIENT)
    assert light.effect == EFFECT_COLOR_LOOP
    assert light.rgb_color is None
    await light.async_turn_on(**{ATTR_EFFECT: EFFECT_OFF})
    assert coordinator.command_batches[-1] == [b"SCFFFFFF80"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {ATTR_BRIGHTNESS: -1},
        {ATTR_BRIGHTNESS: 256},
        {ATTR_BRIGHTNESS: True},
        {ATTR_RGB_COLOR: (1, 2)},
        {ATTR_RGB_COLOR: (1, 2, 256)},
        {ATTR_EFFECT: "Random"},
    ],
)
async def test_invalid_light_requests_fail_before_ble(coordinator, kwargs):
    light = HatchRestLight(coordinator)
    with pytest.raises(ServiceValidationError):
        await light.async_turn_on(**kwargs)
    coordinator.async_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_volume_zero_sends_valid_command(coordinator):
    player = HatchRestMediaPlayer(coordinator)
    await player.async_set_volume_level(0.0)
    assert coordinator.command_batches == [[b"SV00"]]


@pytest.mark.asyncio
async def test_volume_half_byte_rounds_like_android(coordinator):
    player = HatchRestMediaPlayer(coordinator)
    await player.async_set_volume_level(0.3)
    assert coordinator.command_batches == [[b"SV4D"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "volume, command", [(0, b"SV08"), (0.5, b"SV84"), (1, b"SVFF")]
)
async def test_second_legacy_revision_volume_uses_fresh_hardware(
    coordinator, volume, command
):
    """An initial stale normal scale must not produce a revision-two command."""
    player = HatchRestMediaPlayer(coordinator)
    coordinator.fresh_data = replace(
        coordinator.data, hardware_version="2.0", volume_min=8
    )
    await player.async_set_volume_level(volume)
    assert coordinator.command_batches == [[command]]


@pytest.mark.asyncio
@pytest.mark.parametrize("volume", [-0.1, 1.1, float("nan"), float("inf"), True])
async def test_invalid_volume_fails_before_ble(coordinator, volume):
    player = HatchRestMediaPlayer(coordinator)
    with pytest.raises(ServiceValidationError):
        await player.async_set_volume_level(volume)
    coordinator.async_control.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_pause_preserves_the_fresh_selected_sound(coordinator):
    player = HatchRestMediaPlayer(coordinator)
    coordinator.fresh_data = replace(coordinator.data, sound=7)
    await player.async_media_pause()
    coordinator.data = replace(coordinator.data, sound=0)
    await player.async_media_pause()
    await player.async_media_play()
    assert coordinator.command_batches == [[b"SN00"], [], [b"SN07"]]


@pytest.mark.asyncio
async def test_play_after_startup_without_sound_defaults_to_pink_noise(coordinator):
    coordinator.data = replace(coordinator.data, sound=0, power=False)
    player = HatchRestMediaPlayer(coordinator)
    await player.async_media_play()
    assert coordinator.command_batches == [[b"SI01", b"SN03"]]


@pytest.mark.asyncio
async def test_play_keeps_a_fresh_active_sound(coordinator):
    player = HatchRestMediaPlayer(coordinator)
    coordinator.fresh_data = replace(coordinator.data, sound=9)
    await player.async_media_play()
    assert coordinator.command_batches == [[]]


@pytest.mark.asyncio
async def test_select_source_powers_on_then_selects_sound(coordinator):
    coordinator.data = replace(coordinator.data, power=False)
    player = HatchRestMediaPlayer(coordinator)
    await player.async_select_source(SOUNDS[7])
    assert coordinator.command_batches == [[b"SI01", b"SN07"]]


@pytest.mark.asyncio
async def test_select_none_preserves_resume_sound(coordinator):
    player = HatchRestMediaPlayer(coordinator)
    await player.async_select_source(SOUNDS[0])
    coordinator.data = replace(coordinator.data, sound=0)
    await player.async_media_play()
    assert coordinator.command_batches == [[b"SN00"], [b"SN05"]]


@pytest.mark.asyncio
async def test_resume_uses_reported_sound_when_selection_is_not_applied(coordinator):
    player = HatchRestMediaPlayer(coordinator)
    coordinator.reported_data = replace(coordinator.data, sound=9)
    await player.async_select_source(SOUNDS[7])
    coordinator.data = replace(coordinator.data, sound=0)
    await player.async_media_play()
    assert coordinator.command_batches == [[b"SN07"], [b"SN09"]]


@pytest.mark.asyncio
async def test_unknown_sound_can_be_reported_and_resumed(coordinator):
    coordinator.data = replace(coordinator.data, sound=250)
    player = HatchRestMediaPlayer(coordinator)
    assert player.source == "Unknown sound (250)"
    assert player.source in player.source_list
    await player.async_media_pause()
    coordinator.data = replace(coordinator.data, sound=0)
    await player.async_media_play()
    assert coordinator.command_batches == [[b"SN00"], [b"SNFA"]]


@pytest.mark.asyncio
async def test_invalid_source_has_an_actionable_error(coordinator):
    player = HatchRestMediaPlayer(coordinator)
    with pytest.raises(ServiceValidationError, match="Choose a source"):
        await player.async_select_source("Uploaded audio")
    coordinator.async_control.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "cached_power", "fresh_power", "expected"),
    [
        ("async_turn_on", True, False, [b"SI01"]),
        ("async_turn_on", False, True, []),
        ("async_turn_off", False, True, [b"SI00"]),
        ("async_turn_off", True, False, []),
    ],
)
async def test_media_power_compares_fresh_state(
    coordinator, method, cached_power, fresh_power, expected
):
    """Native power writes depend on the transaction's read, not stale UI state."""
    coordinator.data = replace(coordinator.data, power=cached_power)
    player = HatchRestMediaPlayer(coordinator)
    coordinator.fresh_data = replace(coordinator.data, power=fresh_power)
    await getattr(player, method)()
    assert coordinator.command_batches == [expected]


@pytest.mark.asyncio
async def test_media_power_does_not_overwrite_firmware_preset(coordinator):
    player = HatchRestMediaPlayer(coordinator)
    coordinator.reported_data = replace(coordinator.data, power=False)
    await player.async_turn_off()
    assert player.state == MediaPlayerState.OFF
    firmware_preset = replace(
        coordinator.data,
        power=True,
        color=(10, 20, 30),
        brightness=200,
        sound=7,
        volume=50,
    )
    coordinator.reported_data = firmware_preset
    await player.async_turn_on()
    assert coordinator.command_batches == [[b"SI00"], [b"SI01"]]
    assert coordinator.data == firmware_preset


@pytest.mark.asyncio
async def test_inherited_media_toggle_powers_off_silent_lamp_then_powers_on(
    coordinator,
):
    """An idle lamp-only Rest is on, so the first toggle must use native off."""
    player = HatchRestMediaPlayer(coordinator)
    coordinator.data = replace(coordinator.data, sound=0)
    assert player.state == MediaPlayerState.IDLE
    coordinator.reported_data = replace(coordinator.data, power=False)
    await player.async_toggle()
    assert player.state == MediaPlayerState.OFF
    assert not coordinator.data.power

    coordinator.reported_data = replace(coordinator.data, power=True)
    await player.async_toggle()
    assert player.state == MediaPlayerState.IDLE
    assert coordinator.data.power
    assert coordinator.data.brightness == 128
    assert coordinator.command_batches == [[b"SI00"], [b"SI01"]]


@pytest.mark.asyncio
async def test_media_turn_on_uses_only_native_power_command(coordinator):
    coordinator.data = replace(
        coordinator.data,
        power=False,
        color=(255, 100, 10),
        brightness=220,
        volume=64,
        sound=7,
    )
    player = HatchRestMediaPlayer(coordinator)
    await player.async_turn_on()
    assert coordinator.command_batches == [[b"SI01"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["async_media_pause", "select_none"])
async def test_sound_off_controls_never_wake_a_globally_off_rest(coordinator, method):
    coordinator.data = replace(coordinator.data, power=False)
    player = HatchRestMediaPlayer(coordinator)
    if method == "select_none":
        await player.async_select_source(SOUNDS[0])
    else:
        await getattr(player, method)()
    assert coordinator.command_batches == [[b"SN00"]]


@pytest.mark.asyncio
async def test_pause_leaves_lamp_on_and_resumes_previous_sound(coordinator):
    player = HatchRestMediaPlayer(coordinator)
    light = HatchRestLight(coordinator)
    coordinator.reported_data = replace(coordinator.data, sound=0)
    await player.async_media_pause()
    assert player.state == MediaPlayerState.IDLE
    assert light.is_on
    coordinator.reported_data = replace(coordinator.data, sound=5)
    await player.async_media_play()
    assert player.state == MediaPlayerState.PLAYING
    assert light.is_on
    assert coordinator.command_batches == [[b"SN00"], [b"SN05"]]


@pytest.mark.asyncio
async def test_light_wake_keeps_firmware_reported_sound_and_volume(coordinator):
    coordinator.data = replace(coordinator.data, power=False, sound=7)
    light = HatchRestLight(coordinator)
    player = HatchRestMediaPlayer(coordinator)
    coordinator.reported_data = replace(
        coordinator.data, power=True, brightness=100, sound=9, volume=20
    )
    await light.async_turn_on(**{ATTR_BRIGHTNESS: 100})
    assert coordinator.command_batches == [[b"SI01", b"SC14283C64"]]
    assert player.source == SOUNDS[9]
    assert player.volume_level == pytest.approx(20 / 255)


@pytest.mark.asyncio
async def test_sound_wake_keeps_firmware_lamp_and_volume_settings(coordinator):
    coordinator.data = replace(coordinator.data, power=False, brightness=128)
    light = HatchRestLight(coordinator)
    player = HatchRestMediaPlayer(coordinator)
    coordinator.reported_data = replace(
        coordinator.data, power=True, color=(100, 50, 10), brightness=200, volume=50
    )
    await player.async_media_play()
    assert coordinator.command_batches == [[b"SI01", b"SN05"]]
    assert light.rgb_color == (100, 50, 10)
    assert light.brightness == 200
    assert player.volume_level == pytest.approx(50 / 255)
