# Changelog

## 0.4.0 — 2026-11-04

- Each native program is now its own device under the Rest, `<Rest> program <slot>`, with entities for Enabled, repeat days (Monday–Sunday), Name, Start time, Duration, Color, Brightness, Volume, Sound and, on firmware 1.0.4.31+, Button lock. Program devices are HA child devices: they share the Rest's area, are disabled with it, and are removed with it.
- Changes to one program within about a second are written as one verified save; entities show only device-confirmed values, and unchanged displayed values keep their exact native values.
- New **Add program** button on the Rest creates a program in the first free slot. Deleting a program device deletes the program from the Rest first.
- A program conflict from an entity reloads the program table instead of leaving it unavailable until a manual refresh.
- The existing program enable switch keeps its entity ID and history, moves to its program device and is renamed **Enabled**.
- "No connectable Bluetooth route to this Rest" is replaced by a message that names the likely cause — the Hatch app holding the Rest's only connection, power or range, or only passive scanners hearing it — followed by HA's Bluetooth reachability detail. A test now covers HA retrying setup as soon as the Rest advertises again.

## 0.3.0 — 2026-11-03

**Action may be needed:** entries that never changed the polling interval now poll every 60 seconds instead of 10, so app and button changes take up to a minute to appear unless a connection is being held. Choose **Configure → Updates and connection** to set 15–300 seconds. Saved intervals below 15 seconds run at 15.

- Reduce Bluetooth traffic. Each poll is a full connection; on ESP32 proxies that occupies a connection slot and interrupts scanning, which affected every other Bluetooth device on the proxy at the old 10-second default.
- Follow the Rest's advertisements: read it immediately when an unreachable Rest advertises again, and mark entities unavailable when HA stops hearing it. Advertisement-data changes are logged at debug level.
- Add **Keep connected after a command** (0–120 seconds, default 0). While held, follow-up commands reuse the connection and feedback notifications push button presses to HA immediately. Polls reuse a held connection but never extend it.
- Program-table reads go one slot at a time on a shared connection, so polls and commands run between slots instead of waiting for the whole table. The background table refresh is now hourly.
- Bound live operations: polls make at most two connection attempts, commands three, and a poll or command fails within 60 seconds including any wait for another operation. A link dropped mid-request fails that request immediately.
- Read the firmware revision once and show it in the device registry. Add a disabled-by-default **Signal strength** diagnostic sensor with the receiving adapter/proxy as its `source` attribute.
- Merge the duplicated live-control and program session code into one runner; the no-replay guarantee after a write is unchanged.
- Packaging for the Home Assistant Marketplace: manifest categories and key order, HACS and hassfest workflows, and a test workflow that now actually runs the Home Assistant suite (it previously failed to import HA's Bluetooth component).

## 0.2.0 — 2026-10-15

- Read and manage the original Rest's ten native program slots, including programs created in the Hatch app. The Rest remains the source of truth; HA does not store another schedule or execute program start/end actions.
- Add **Configure → Manage programs** with a program selector, **Add a new program** and refresh, then edit, enable/disable, and delete actions. One HA form covers the verified fields and firmware-supported toddler lock. Poll interval settings move to **Configure → Polling interval**.
- Add Configuration switches for occupied program slots and a **Refresh programs** button. Program switches arm scheduled execution and do not control the current light/sound or replace whole-device power. Slot-based unique IDs remain stable when a program is renamed.
- Read programs after setup, on explicit refresh/editor operations, and every 15 minutes. Program and live-control sessions share one serialized BLE client; live polling remains 10 seconds by default.
- Require fresh full-record baseline checks for save, delete, and enable/disable, including apparent no-ops and slot reuse. Conflicts report an editor/action error and make the program table unavailable until refreshed. Require command acknowledgements and post-commit readback for mutations. Never replay a transaction after its first mutation write; report uncertain partial writes and require a refresh before another attempt.
- Read the native clock for information without setting the clock or changing DST rules. Preserve the 0.1.5 Light/Sound power behavior, existing Light/Sound IDs, and approved Hatch branding.

Replace `custom_components/hatch_rest_ble`, including `brand`, and restart Home Assistant. Upgrading reads existing programs without writing them. Record the app's current program settings and test an unused slot before relying on the new editor. Full HA and hardware checks remain pending; see [program usage](docs/PROGRAMS.md) and [validation status](docs/VALIDATION.md). Calendar integration, fades, multistep routines, and monthly/exception scheduling are not implemented.

## 0.1.5 — 2026-10-15

- Move whole-device power to media-player On/Off using only native `SI01`/`SI00` when fresh device state requires a change. Firmware handles restoration; HA saves no master settings snapshot and adds no color, sound, or volume writes to power actions.
- Remove the separate Power switch and remove only its obsolete entity-registry entry after successful setup. Preserve Light/Sound entity IDs and local Hatch branding.
- Report media state as Off when whole-device power is off, Idle when powered with source None, and Playing when powered with a selected sound. The media power toggle can turn off a lamp-only Rest.
- Keep Pause and source None sound-only through `SN00`. Play and source selection retain sound resume behavior.
- Revert the forced output isolation introduced in 0.1.3 following reports of unexpected device behavior. Light wake now sends `SI01` then requested `SC`; sound wake sends `SI01` then selected `SN`, without suppressing the other output or reapplying volume. Native wake may restore the other output. The cause of the reported behavior has not been confirmed on hardware.

Replace `custom_components/hatch_rest_ble`, including `brand`, and restart Home Assistant. Update old Power-switch automations to `media_player.turn_on`/`media_player.turn_off`; use `media_player.media_pause` for sound-only silence. Existing device entries and Light/Sound entity IDs are retained. Full Home Assistant and physical-device validation remain pending; see [validation status](docs/VALIDATION.md).

## 0.1.4 — 2026-10-15

- Add the approved Hatch logo as local Home Assistant branding at 256×256 and 512×512 pixels. Trim excess outer padding while preserving the artwork's proportions and colors.
- Keep device controls and Bluetooth behavior unchanged from 0.1.3.

Upgrade by replacing `custom_components/hatch_rest_ble`, including its `brand` folder, and restarting Home Assistant. Existing entries and entity IDs are retained. See [branding attribution](docs/BRANDING.md) and [validation status](docs/VALIDATION.md).

## 0.1.3 — 2026-10-15

- Wake the light independently with `SI01`, `SN00`, preserved volume, then the requested color and brightness. Wake sound independently with `SI01`, a dark lamp, preserved volume, then the selected sound. Changes while already powered on preserve the other active output.
- Make media-player On/Play audio-only and Off/Pause sound-only. Off/Pause sends `SN00` and never sends the shared `SI00` power-off command. Zero brightness, None, and pause do not wake a powered-off Rest.
- Keep the Power switch's ordinary `SI00`/`SI01` behavior, relying on the Rest's firmware to restore its own settings. No separate master settings snapshot is stored in HA.
- Add regression coverage for independent activation, preservation of the other output, and resume memory.

Upgrade by replacing `custom_components/hatch_rest_ble` and restarting Home Assistant. Existing entries and entity IDs are retained. The 0.5-second delay after each write and confirmed state publication before disconnect cleanup are unchanged. Shared hardware wake may briefly revive the previous preset before the following commands suppress the other output; zero sound blips or lamp flashes are not guaranteed. Full Home Assistant and physical-device validation remain pending; see [validation status](docs/VALIDATION.md).

## 0.1.2 — 2026-10-12

- Reduce the default polling interval from 60 to 10 seconds. Add a per-device interval option from 5 to 300 seconds; saving it reloads the entry.
- Publish successful command feedback as soon as it is read, before waiting for proxy disconnection. The service call still waits for cleanup, and commands remain serialized for each Rest.
- Add debug timing for operation lock waits, connection, feedback reads, and disconnection to help diagnose delays.
- Retain short BLE sessions and the 0.5-second delay after each command write. App and button changes appear on the next poll plus connection time; HA commands start without waiting for that poll.

Upgrade by replacing `custom_components/hatch_rest_ble` and restarting Home Assistant. Existing entries and entity IDs are retained. Physical-device and full Home Assistant validation remain pending; see [validation status](docs/VALIDATION.md).

## 0.1.1 — 2026-10-11

- Clear stale GATT services on the connected client when reads encounter invalid handles, missing or incompatible characteristics, or malformed feedback. Reconnect once with fresh services before reporting a read failure.
- Avoid replaying an operation after a command write has begun. A later poll can refresh reported state and recover availability.
- Bound connection and operation timeouts, and retain a possibly connected client for cleanup when disconnect fails before opening another connection. Release an already disconnected proxy client even if its disconnect RPC failed.
- Recognize the alternate legacy `13 37 R/r` manufacturer signature under HA/Bleak key 14099, alongside key 1076.
- Add a manual Bluetooth MAC address field for a Rest missing from the chooser, using HA's shared connectable route and the same first-generation GATT verification.
- Document that a plugged-in Rest can be added with its light and sound off. Setup verification and polling do not power it on.

Upgrade by replacing `custom_components/hatch_rest_ble` and restarting Home Assistant. Existing entries and entity IDs are retained. Cache recovery has not yet been verified on the affected physical hardware; see [validation status](docs/VALIDATION.md) for executed tests and remaining checks.

## 0.1.0 — 2026-10-10

- Initial integration for the original BLE Rest family using Home Assistant's Bluetooth stack.
- RGB light, brightness, Color loop, built-in sounds, volume, and whole-device power.
- Tagged feedback parsing and legacy hardware-revision volume scaling informed by the supplied Hatch Sleep 6.40.1 Android app.
- Protocol, transport, and Home Assistant regression suites, plus a physical-device validation plan.
