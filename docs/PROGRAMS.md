# Native Rest programs

Version 0.2.0 manages the original BLE Rest's ten program slots. It reads the same device records used by the Hatch app. A program contains a repeating start time, duration, light/sound settings, and an enabled flag. The Rest is the source of truth; the integration keeps a working copy in memory without saving a second schedule in HA options or persistent storage.

Native program execution is strongly indicated by the app's BLE protocol. Operation through HA/proxy outages, retention after unplugging, and behavior across all firmware revisions still need physical-device verification. Local verification passed 227 unit tests and Ruff lint/formatting for all 35 Python files. The actual HA suite and physical-device tests remain unrun. See [VALIDATION.md](VALIDATION.md).

## Open the editor

1. Open **Settings → Devices & services**, find **Hatch Rest BLE (1st Gen)**, and choose **Configure** for the relevant Rest.
2. Choose **Manage programs**. The list contains occupied slots with name and schedule summaries, plus **Add a new program** and **Refresh programs from the Rest**.
3. Select an existing program, then choose **Edit program**, **Enable or disable program**, or **Delete program**. **Add a new program** uses an available slot; there are ten slots in total.
4. Edit the fields in one HA form and submit it. Saving requires a BLE connection, application acknowledgements, and a readback of the saved slot.

This is a standard HA options flow, using menus and forms. It does not add the custom cards shown in the design preview. **Configure → Updates and connection** holds the separate live polling and connection settings. HA documents this UI mechanism in its [options flow guidance](https://developers.home-assistant.io/docs/core/integration/options_flow/).

Before the first write, record the existing program settings in the Hatch app and test an unused slot. Close the app to release its BLE connection while HA connects. Reopen it after HA has disconnected to compare the saved result.

## Program fields

| Field | Supported input and meaning |
| --- | --- |
| Name | 1–16 bytes of printable ASCII, with at least one non-space character, for new programs and full saves. Reply tokens `OK`, `E01`–`E06`, and `Unknown` are reserved. Existing names decoded from UTF-8 remain readable and can be enabled, disabled, or deleted without rewriting the name. To fully edit such a program, supply an ASCII name. |
| Enabled | Arms the program for scheduled starts. It does not start the program immediately or describe whether it is currently running. |
| Repeat days | The selected days refer to the start time. Native masks use Sunday as the first bit. An empty day mask is not treated as a one-shot calendar event. |
| Start | A local wall-clock time interpreted by the Rest. The protocol carries no IANA timezone or UTC offset for a program. |
| Duration | 1 minute through 17 hours 59 minutes. The form edits hours/minutes. An existing raw duration containing seconds is preserved if the duration fields are unchanged. |
| Color and brightness | RGB color and brightness percentage. Brightness is encoded as the native 0–255 byte. |
| Sound and volume | Built-in sound selection and volume percentage. Volume uses the device's hardware-revision scale, including the 8–255 raw range for revision strings beginning with `2`. A minimum volume on that revision may remain audible; choose None for a silent program. |
| Toddler lock | Available only when a known numeric firmware version is at least `10431`. Unsupported or unknown firmware does not expose this write. This is the app's whole-lock setting, not a per-button editor. |

The full editor preserves existing protocol fields that it does not expose. It does not invent calendar exceptions, monthly recurrence, fades, or multistep routines. There is no calendar entity in this release.

## Program switches and refresh

Each occupied slot gets a **Configuration** switch named for its program. On means the program is enabled for future scheduled starts. Off disables it. Before changing only the enabled flag, the integration compares the freshly read complete program against its cached record. A changed or replaced slot reports a conflict, including when its current enabled state already matches the requested state. These switches are separate from the Sound media player's whole-device power control.

The switch unique ID is based on `<MAC>:program:<slot>`, so renaming a program keeps its identity. Empty slots do not create switches. Deleting a program removes that slot's switch; Light/Sound and other slots remain. A new program in a reused slot uses the slot's identity.

The device's **Refresh programs** button reads all slots from the Rest; **Refresh programs from the Rest** in the editor does the same. The program table also loads in the background after setup and refreshes hourly; entering or refreshing the editor obtains device data as well. Use explicit refresh to see a change made in the app immediately. A table read takes one slot at a time on a shared connection, so live controls can run between slots. Live light/sound polling remains independently configurable, with a 60-second default.

All sessions use the same per-device BLE lock. A program read or write can hold up another action while its transaction completes. Avoid repeated refreshes while troubleshooting command latency, and use debug timings to distinguish queued work from connection or read delays.

## Changes made in the app

HA imports existing native programs rather than creating replacements at startup. Save, delete, and enable/disable all compare a fresh complete slot read against a baseline. The editor retains the record selected for editing or confirmation; switches use the cached program record. If the app or another operation changed or replaced it meanwhile, HA reports a conflict before a mutation or an apparent no-op. An enable/disable change still writes only the native enabled flag once the baseline matches.

An editor conflict remains on the form; a switch conflict reports a HA action error. The program table becomes unavailable until a successful refresh. Use **Refresh programs** or the editor's Reload/refresh option, then review the latest program before trying again. Reload in the editor discards unsaved edits.

Program edits do not apply the light/sound settings as immediate remote-control commands. Hatch documents that changes during a running program affect its next run, and a newly created program whose start time has passed waits for its next scheduled start. [Hatch: Setting Programs](https://help.hatch.co/hc/en-us/articles/115001825327-Setting-Programs).

## Clock, overnight programs, and DST

Start time is local wall time plus a duration. A displayed end time is a convenience; it is not a separately stored fixed wall-clock deadline. A program starting late on a selected day can continue into the following day.

The integration reads the Rest's clock with `GT` for information. It does not set the clock, transmit timezone rules, or automatically adjust programs for DST. Check device time if a program starts at an unexpected hour. In the Hatch app, connect to the Rest, open its settings, select the device, and use **Device Time → Sync Time** when needed.

Hatch's classic Rest guidance describes duration-based overnight behavior across time changes and says a later-starting overlapping routine overrides the earlier one. Verify overnight timing and overlaps on the device; the editor does not calculate or apply DST corrections. [Hatch: Daylight Savings and Your Hatch Devices](https://help.hatch.co/hc/en-us/articles/115003020554-Daylight-Savings-and-Your-Hatch-Devices).

HA's [Schedule helper](https://www.home-assistant.io/integrations/schedule/) is a separate weekly trigger/condition for automations. Creating one does not write native Rest programs. This integration does not run a parallel HA scheduler for its program table.

## Errors and uncertain writes

Program commands require an explicit device response; a successful Bluetooth write alone is insufficient. HA rejects device error statuses, malformed responses, oversized or incomplete data, and timeouts. A full save finishes only after reading back and matching the expected saved fields. Delete similarly verifies the slot is empty.

After the first mutation command begins, the integration never automatically replays the transaction. A failure can leave the slot partly changed because the firmware's rollback and atomicity have not been established. Refresh the table and inspect the affected program in HA and the app before another attempt. An uncertain readback must not be interpreted as a verified save or proof that the original values remain.

Program operations do not change the existing native light/media power behavior. Media On/Off controls whole-device power; Pause/None silences sound. Program enable switches arm scheduled execution and do not turn the current lamp/audio on or off.

Implementation evidence, command framing, and known limits are recorded in [PROTOCOL.md](PROTOCOL.md). The APK, DEX files, decompiled app code, and extracted app assets are not distributed.
