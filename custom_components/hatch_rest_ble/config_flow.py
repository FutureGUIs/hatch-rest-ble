"""Configuration flow for first-generation Hatch Rest devices."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import replace
from datetime import datetime, time, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .api import HatchRestClient, HatchRestError, UnsupportedDeviceError
from .const import (
    CONF_POLL_INTERVAL,
    DOMAIN,
    MANUFACTURER_IDS,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
    POLL_INTERVAL_SECONDS,
    SOUNDS,
)
from .program_coordinator import ProgramConflictError
from .programs import (
    FLAG_ENABLED,
    FLAG_EXISTS,
    MAX_DURATION,
    MIN_DURATION,
    PROGRAM_SLOTS,
    TODDLER_LOCK_MASK,
    RestProgram,
)

_LOGGER = logging.getLogger(__name__)
CONF_MANUAL_ADDRESS = "manual_address"
WEEKDAYS = (
    ("mon", "Monday", 0x02),
    ("tue", "Tuesday", 0x04),
    ("wed", "Wednesday", 0x08),
    ("thu", "Thursday", 0x10),
    ("fri", "Friday", 0x20),
    ("sat", "Saturday", 0x40),
    ("sun", "Sunday", 0x01),
)
_MAC_ADDRESS = re.compile(
    r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}|(?:[0-9a-f]{2}-){5}[0-9a-f]{2}|[0-9a-f]{12}",
    re.IGNORECASE,
)


def _canonical_address(address: str) -> str:
    """Use the full Bluetooth address consistently for all flow sources."""
    return address.upper()


def _manual_address(address: str) -> str | None:
    """Validate a complete MAC and normalize the common input formats."""
    address = address.strip()
    if not _MAC_ADDRESS.fullmatch(address):
        return None
    compact = address.replace(":", "").replace("-", "").upper()
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def _is_candidate(info: BluetoothServiceInfoBleak) -> bool:
    """Match the app's classic Rest R/r marker before GATT verification."""
    return (
        info.connectable
        and any(
            info.manufacturer_data.get(manufacturer_id, b"").startswith((b"R", b"r"))
            for manufacturer_id in MANUFACTURER_IDS
        )
        and info.name not in {"RestDFU", "NOSDDFU"}
    )


class HatchRestConfigFlow(ConfigFlow, domain=DOMAIN):
    """Discover a Rest and verify its first-generation GATT protocol."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HatchRestOptionsFlow:
        """Manage native programs and configure polling."""
        return HatchRestOptionsFlow()

    def __init__(self) -> None:
        self._address: str | None = None
        self._name: str | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle HA discovery without connecting to an unselected device."""
        if not _is_candidate(discovery_info):
            return self.async_abort(reason="unsupported_device")
        await self.async_set_unique_id(_canonical_address(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._address = _canonical_address(discovery_info.address)
        self._name = discovery_info.name
        return await self.async_step_confirm()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select a discovered Rest or enter a Bluetooth address manually."""
        errors: dict[str, str] = {}
        if user_input is not None:
            selected = user_input.get(CONF_ADDRESS)
            manual = user_input.get(CONF_MANUAL_ADDRESS, "").strip()
            if bool(selected) == bool(manual):
                errors["base"] = "select_one_device"
            elif manual:
                if address := _manual_address(manual):
                    await self.async_set_unique_id(address)
                    self._abort_if_unique_id_configured()
                    self._address = address
                    self._name = None
                    return await self.async_step_confirm()
                errors[CONF_MANUAL_ADDRESS] = "invalid_address"
            elif discovery := self._discovered.get(_canonical_address(selected)):
                address = _canonical_address(discovery.address)
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                self._address = address
                self._name = discovery.name
                return await self.async_step_confirm()
            else:
                errors["base"] = "select_one_device"

        configured_ids = self._async_current_ids()
        self._discovered = {
            _canonical_address(info.address): info
            for info in bluetooth.async_discovered_service_info(
                self.hass, connectable=True
            )
            if _is_candidate(info)
            and _canonical_address(info.address) not in configured_ids
        }
        choices = {
            address: f"{info.name or 'Hatch Rest'} ({address})"
            for address, info in sorted(self._discovered.items())
        }
        fields: dict[vol.Marker, Any] = {}
        if choices:
            fields[vol.Optional(CONF_ADDRESS)] = vol.In(choices)
        fields[vol.Optional(CONF_MANUAL_ADDRESS)] = str
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(fields),
            errors=errors,
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate only the selected device after user confirmation."""
        assert self._address is not None
        address = self._address
        name = self._name or f"Hatch Rest {address[-5:]}"
        self.context["title_placeholders"] = {"name": name, "address": address}
        errors: dict[str, str] = {}
        if user_input is not None:
            client = HatchRestClient(self.hass, address, name)
            try:
                await client.async_update()
            except UnsupportedDeviceError:
                return self.async_abort(reason="unsupported_device")
            except HatchRestError:
                errors["base"] = "cannot_connect"
            except Exception:  # Do not hide programmer errors in the log.
                _LOGGER.exception("Unexpected error verifying Hatch Rest")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=name,
                    data={CONF_ADDRESS: address, CONF_NAME: name},
                )
            finally:
                await client.async_close()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="confirm",
            errors=errors,
            description_placeholders={"name": name, "address": address},
        )


class HatchRestOptionsFlow(OptionsFlowWithReload):
    """Edit programs on the Rest; persist only integration preferences."""

    def __init__(self) -> None:
        self._snapshot: Any = None
        self._expected: RestProgram | None = None
        self._draft: RestProgram | None = None
        self._notice = ""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep integration preferences separate from device program actions."""
        return self.async_show_menu(
            step_id="init", menu_options=["programs", "polling"]
        )

    def _program_coordinator(self) -> Any:
        """A device editor needs an active entry and its shared coordinator."""
        if self.config_entry.state != ConfigEntryState.LOADED:
            return None
        runtime = getattr(self.config_entry, "runtime_data", None)
        return getattr(runtime, "program_coordinator", None)

    async def _refresh_programs(self) -> None:
        coordinator = self._program_coordinator()
        await coordinator.async_refresh_programs()
        if coordinator.data is None:
            raise HomeAssistantError("The Rest did not return its program table")
        self._snapshot = coordinator.data

    def _clock_summary(self) -> str:
        clock = self._snapshot.device_time if self._snapshot is not None else None
        if clock is None:
            return "Unavailable; new programs use Home Assistant's local date."
        return f"{clock:%Y-%m-%d %H:%M:%S} (Rest local time)"

    @staticmethod
    def _summary(program: RestProgram) -> str:
        days = (
            ", ".join(label[:3] for _key, label, bit in WEEKDAYS if program.days & bit)
            or "No weekdays (Never)"
        )
        end = program.start + timedelta(seconds=program.duration)
        next_day = " (+1 day)" if end.date() > program.start.date() else ""
        hours, remainder = divmod(program.duration, 3600)
        minutes, seconds = divmod(remainder, 60)
        duration = f"{hours} h {minutes} min" + (f" {seconds} s" if seconds else "")
        status = "Enabled" if program.enabled else "Disabled"
        return (
            f"{program.slot}. {program.name} · {status} · {days} · "
            f"{program.start:%H:%M:%S}–{end:%H:%M:%S}{next_day} · {duration}"
        )

    def _program_list(self, errors: dict[str, str]) -> ConfigFlowResult:
        options = [{"value": "refresh", "label": "Refresh programs from the Rest"}]
        summary = "No programs read yet."
        if self._snapshot is not None:
            programs = sorted(
                (item for item in self._snapshot.programs if item.exists),
                key=lambda item: item.slot,
            )
            summary = (
                "\n".join(self._summary(item) for item in programs) or "No programs."
            )
            if not errors:
                options = [
                    {"value": str(item.slot), "label": self._summary(item)}
                    for item in programs
                ]
                if len(programs) < PROGRAM_SLOTS:
                    options.append({"value": "add", "label": "Add a new program"})
                options.append(
                    {"value": "refresh", "label": "Refresh programs from the Rest"}
                )
        return self.async_show_form(
            step_id="programs",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "choice", default=options[0]["value"]
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(options=options)
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "programs": summary,
                "device_clock": self._clock_summary(),
                "notice": self._notice,
            },
        )

    async def async_step_programs(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read the complete table before listing programs or allocating a slot."""
        if self._program_coordinator() is None:
            return self.async_abort(reason="device_not_loaded")
        if self._snapshot is None or (
            user_input is not None and user_input["choice"] == "refresh"
        ):
            try:
                await self._refresh_programs()
            except HomeAssistantError:
                return self._program_list({"base": "program_read_failed"})
        if user_input is None or user_input["choice"] == "refresh":
            return self._program_list({})
        self._notice = ""
        if user_input["choice"] == "add":
            occupied = {
                program.slot for program in self._snapshot.programs if program.exists
            }
            slot = next(
                (slot for slot in range(1, PROGRAM_SLOTS + 1) if slot not in occupied),
                None,
            )
            if slot is None:
                return self._program_list({"base": "programs_full"})
            clock = self._snapshot.device_time or dt_util.now().replace(tzinfo=None)
            state = self.config_entry.runtime_data.data
            self._expected = None
            self._draft = RestProgram(
                slot=slot,
                name="New program",
                start=datetime.combine(clock.date(), time(20)),
                duration=3600,
                days=0x7F,
                color=state.color,
                brightness=state.brightness,
                sound=state.sound,
                volume=state.volume,
                flags=FLAG_EXISTS | FLAG_ENABLED,
                buttons=0 if self._snapshot.supports_toddler_lock else None,
            )
            return await self.async_step_edit_program()
        self._expected = self._snapshot.get(int(user_input["choice"]))
        if self._expected is None or not self._expected.exists:
            self._expected = None
            return self._program_list({"base": "program_missing"})
        self._draft = self._expected
        return await self.async_step_program_actions()

    async def async_step_program_actions(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer actions on the selected device record."""
        if self._program_coordinator() is None:
            return self.async_abort(reason="device_not_loaded")
        if self._expected is None:
            return await self.async_step_programs()
        return self.async_show_menu(
            step_id="program_actions",
            menu_options=[
                "edit_program",
                "toggle_program",
                "delete_program",
                "programs",
            ],
            description_placeholders={"program": self._summary(self._expected)},
        )

    def _editor_values(self) -> dict[str, Any]:
        program = self._draft
        assert program is not None
        minimum = self._snapshot.volume_min
        return {
            "name": program.name,
            "enabled": program.enabled,
            "weekdays": [key for key, _label, bit in WEEKDAYS if program.days & bit],
            "start_time": program.start.strftime("%H:%M:%S"),
            "duration_hours": program.duration // 3600,
            "duration_minutes": program.duration % 3600 // 60,
            "color": list(program.color),
            "brightness": math.floor(program.brightness * 100 / 255 + 0.5),
            "sound": str(program.sound),
            "volume": max(
                0, math.floor((program.volume - minimum) * 100 / (255 - minimum) + 0.5)
            ),
            "toddler_lock": program.toddler_lock,
            "refresh": False,
        }

    def _editor_form(
        self, errors: dict[str, str], submitted: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        defaults = {**self._editor_values(), **(submitted or {}), "refresh": False}
        sounds = [{"value": str(key), "label": label} for key, label in SOUNDS.items()]
        if int(defaults["sound"]) not in SOUNDS:
            sounds.append(
                {
                    "value": defaults["sound"],
                    "label": f"Unknown sound ({defaults['sound']})",
                }
            )
        fields: dict[vol.Marker, Any] = {
            vol.Required("name", default=defaults["name"]): str,
            vol.Required(
                "enabled", default=defaults["enabled"]
            ): selector.BooleanSelector(),
            vol.Required(
                "weekdays", default=defaults["weekdays"]
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    multiple=True,
                    options=[
                        {"value": key, "label": label} for key, label, _bit in WEEKDAYS
                    ],
                )
            ),
            vol.Required(
                "start_time", default=defaults["start_time"]
            ): selector.TimeSelector(),
            vol.Required(
                "duration_hours", default=defaults["duration_hours"]
            ): self._number_selector(0, 17),
            vol.Required(
                "duration_minutes", default=defaults["duration_minutes"]
            ): self._number_selector(0, 59),
            vol.Required(
                "color", default=defaults["color"]
            ): selector.ColorRGBSelector(),
            vol.Required(
                "brightness", default=defaults["brightness"]
            ): self._number_selector(0, 100),
            vol.Required("sound", default=defaults["sound"]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=sounds)
            ),
            vol.Required("volume", default=defaults["volume"]): self._number_selector(
                0, 100
            ),
        }
        if self._snapshot.supports_toddler_lock:
            fields[vol.Required("toddler_lock", default=defaults["toddler_lock"])] = (
                selector.BooleanSelector()
            )
        fields[vol.Optional("refresh", default=False)] = selector.BooleanSelector()
        return self.async_show_form(
            step_id="edit_program",
            data_schema=vol.Schema(fields),
            errors=errors,
            description_placeholders={
                "program": self._summary(self._draft),
                "device_clock": self._clock_summary(),
                "lock_support": (
                    "Button lock can be changed on this firmware."
                    if self._snapshot.supports_toddler_lock
                    else "Button lock is left unchanged on this firmware."
                ),
            },
        )

    @staticmethod
    def _number_selector(minimum: int, maximum: int) -> selector.NumberSelector:
        return selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=minimum, max=maximum, step=1, mode=selector.NumberSelectorMode.BOX
            )
        )

    def _build_program(
        self, values: dict[str, Any]
    ) -> tuple[RestProgram | None, dict[str, str]]:
        original = self._draft
        assert original is not None
        errors = {}
        name = values["name"]
        if (
            not name.strip()
            or len(name) > 16
            or any(not 32 <= ord(c) <= 126 for c in name)
            or name in {"OK", "E01", "E02", "E03", "E04", "E05", "E06", "Unknown"}
        ):
            errors["name"] = "invalid_program_name"
        if not re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", values["start_time"]):
            errors["start_time"] = "invalid_program_time"
        try:
            start_time = time.fromisoformat(values["start_time"])
        except ValueError:
            errors["start_time"] = "invalid_program_time"
        for field in ("duration_hours", "duration_minutes", "brightness", "volume"):
            value = values[field]
            if (
                isinstance(value, bool)
                or not math.isfinite(value)
                or value != int(value)
            ):
                errors[field] = "whole_number_required"
        if errors:
            return None, errors
        duration = (
            int(values["duration_hours"]) * 3600 + int(values["duration_minutes"]) * 60
        )
        defaults = self._editor_values()
        if (values["duration_hours"], values["duration_minutes"]) == (
            defaults["duration_hours"],
            defaults["duration_minutes"],
        ):
            duration = (
                original.duration
            )  # Preserve native 30-second offsets on name edits.
        if not MIN_DURATION <= duration <= MAX_DURATION:
            return None, {"duration_minutes": "invalid_program_duration"}
        minimum = self._snapshot.volume_min
        brightness = (
            original.brightness
            if values["brightness"] == defaults["brightness"]
            else math.floor(values["brightness"] * 255 / 100 + 0.5)
        )
        volume = (
            original.volume
            if values["volume"] == defaults["volume"]
            else math.floor(values["volume"] * (255 - minimum) / 100 + 0.5) + minimum
        )
        buttons = original.buttons
        if (
            self._snapshot.supports_toddler_lock
            and values["toddler_lock"] != defaults["toddler_lock"]
        ):
            buttons = ((buttons or 0) & ~TODDLER_LOCK_MASK) | (
                TODDLER_LOCK_MASK if values["toddler_lock"] else 0
            )
        return replace(
            original,
            name=name,
            flags=(original.flags & ~FLAG_ENABLED)
            | (FLAG_ENABLED if values["enabled"] else 0),
            days=sum(bit for key, _label, bit in WEEKDAYS if key in values["weekdays"]),
            start=datetime.combine(original.start.date(), start_time),
            duration=duration,
            color=tuple(values["color"]),
            brightness=brightness,
            sound=int(values["sound"]),
            volume=volume,
            buttons=buttons,
        ), {}

    async def async_step_edit_program(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep an immutable expected record until the device verifies the save."""
        coordinator = self._program_coordinator()
        if coordinator is None:
            return self.async_abort(reason="device_not_loaded")
        if self._draft is None:
            return await self.async_step_programs()
        if user_input is None:
            return self._editor_form({})
        if user_input.get("refresh"):
            return await self._reload_editor(user_input)
        program, errors = self._build_program(user_input)
        if errors:
            return self._editor_form(errors, user_input)
        try:
            await coordinator.async_save_program(program, expected=self._expected)
        except ProgramConflictError:
            return self._editor_form({"base": "program_conflict"}, user_input)
        except HomeAssistantError:
            return self._editor_form({"base": "program_write_failed"}, user_input)
        self._snapshot = coordinator.data
        self._notice = "Program saved and confirmed on the Rest."
        return self._program_list({})

    async def _reload_editor(self, submitted: dict[str, Any]) -> ConfigFlowResult:
        try:
            await self._refresh_programs()
        except HomeAssistantError:
            return self._editor_form({"base": "program_read_failed"}, submitted)
        assert self._draft is not None
        if self._expected is None:
            occupied = {
                program.slot for program in self._snapshot.programs if program.exists
            }
            slot = next(
                (slot for slot in range(1, PROGRAM_SLOTS + 1) if slot not in occupied),
                None,
            )
            if slot is None:
                return self._program_list({"base": "programs_full"})
            self._draft = replace(self._draft, slot=slot)
        else:
            self._expected = self._snapshot.get(self._draft.slot)
            if self._expected is None or not self._expected.exists:
                self._expected = None
                return self._program_list({"base": "program_missing"})
            self._draft = self._expected
        return self._editor_form({})

    def _confirmation_form(
        self, step_id: str, errors: dict[str, str]
    ) -> ConfigFlowResult:
        assert self._expected is not None
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required("confirm", default=False): selector.BooleanSelector(),
                    vol.Optional("refresh", default=False): selector.BooleanSelector(),
                }
            ),
            errors=errors,
            description_placeholders={
                "program": self._summary(self._expected),
                "action": "Disable" if self._expected.enabled else "Enable",
            },
        )

    async def _program_confirmation(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        coordinator = self._program_coordinator()
        if coordinator is None:
            return self.async_abort(reason="device_not_loaded")
        if self._expected is None:
            return await self.async_step_programs()
        if user_input is None:
            return self._confirmation_form(step_id, {})
        if user_input.get("refresh"):
            try:
                await self._refresh_programs()
            except HomeAssistantError:
                return self._confirmation_form(step_id, {"base": "program_read_failed"})
            self._expected = self._snapshot.get(self._expected.slot)
            if self._expected is None or not self._expected.exists:
                self._expected = None
                return self._program_list({"base": "program_missing"})
            self._draft = self._expected
            return self._confirmation_form(step_id, {})
        if not user_input["confirm"]:
            return self._confirmation_form(
                step_id, {"confirm": "confirmation_required"}
            )
        try:
            if step_id == "delete_program":
                await coordinator.async_delete_program(
                    self._expected.slot, expected=self._expected
                )
                self._notice = "Program deleted and confirmed on the Rest."
            else:
                await coordinator.async_set_program_enabled(
                    self._expected.slot,
                    not self._expected.enabled,
                    expected=self._expected,
                )
                self._notice = "Program enabled state updated on the Rest."
        except ProgramConflictError:
            return self._confirmation_form(step_id, {"base": "program_conflict"})
        except HomeAssistantError:
            return self._confirmation_form(step_id, {"base": "program_write_failed"})
        self._snapshot = coordinator.data
        return self._program_list({})

    async def async_step_delete_program(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._program_confirmation("delete_program", user_input)

    async def async_step_toggle_program(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._program_confirmation("toggle_program", user_input)

    async def async_step_polling(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate polling bounds without opening a Bluetooth session."""
        errors: dict[str, str] = {}
        if user_input is not None:
            interval = user_input.get(CONF_POLL_INTERVAL)
            if (
                isinstance(interval, (int, float))
                and not isinstance(interval, bool)
                and MIN_POLL_INTERVAL_SECONDS <= interval <= MAX_POLL_INTERVAL_SECONDS
                and interval == int(interval)
            ):
                return self.async_create_entry(
                    title="",
                    data={
                        **self.config_entry.options,
                        CONF_POLL_INTERVAL: int(interval),
                    },
                )
            errors[CONF_POLL_INTERVAL] = "invalid_poll_interval"

        return self.async_show_form(
            step_id="polling",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_POLL_INTERVAL, POLL_INTERVAL_SECONDS
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=MIN_POLL_INTERVAL_SECONDS,
                            max=MAX_POLL_INTERVAL_SECONDS,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    )
                }
            ),
            errors=errors,
        )
