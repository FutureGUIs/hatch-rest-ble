# Hatch Rest (original BLE family) protocol notes

This integration targets the original Rest BLE family, including its BLE hardware revisions. It does not target Rest+, Rest Mini, Restore, or Wi-Fi Rest models. The protocol was checked against the user-provided Hatch Sleep 6.40.1 Android XAPK and the reference [hatch_rest_homeassistant repository](https://github.com/jcgoette/hatch_rest_homeassistant). No APK, DEX, extracted app assets, or app source is included in the integration.

XAPK SHA-256: `117988d23a6c7e5f599e81993c6b51f1f20d1414793607012605c39b11502f92`.

## Evidence and limits

The Android application is partially obfuscated. References below give the DEX file, class descriptor, source-file metadata, and method name from this specific build. They describe inspected instructions and resource mappings, not runtime captures from a physical device. Hardware acceptance still needs a real Rest and a supported Home Assistant Bluetooth adapter or active proxy.

## GATT endpoints

| Purpose | UUID | Evidence |
| --- | --- | --- |
| Command TX | `02240002-5efd-47eb-9c1a-de53f7a2b232` | `classes4.dex`, `com.hatchbaby.rest.connectivity.ble.w3`, source `NightlightDevice.java`, `K3`, `d3` |
| Command RX | `02240003-5efd-47eb-9c1a-de53f7a2b232` | Same class, `D3` |
| Feedback state | `02260002-5efd-47eb-9c1a-de53f7a2b232` | Same class, `x3` |
| Hardware revision | `00002a27-0000-1000-8000-00805f9b34fb` | Same class, `z3` |
| Firmware revision | `00002a26-0000-1000-8000-00805f9b34fb` | Same class, `y3` |
| Model number | `00002a24-0000-1000-8000-00805f9b34fb` | Standard Device Information characteristic used by the legacy device class |

Neither the inspected legacy Android path nor the reference integration establishes a required command service UUID. Both find the command and feedback characteristics directly; this integration does the same.

The `023400xx` UUID family also appears in the app's IoT device path and is outside this integration's scope. The UUID `9A04F079-9840-4286-AB92-E65BE0885F95` is an unrelated media DRM UUID in this APK, not a Hatch characteristic.

## Service resolution and recovery in 0.1.1

These are integration implementation choices, not additional facts established by the APK or a physical-device trace:

- Every session obtains a `BLEDevice` from Home Assistant's shared Bluetooth manager and resolves the command and feedback UUIDs from that session's services. This also applies to manually entered addresses.
- An invalid-handle ATT error, missing or incompatible control characteristics, or malformed feedback invalidates the GATT cache on the live client before disconnecting. Calling the client's `clear_cache()` targets the selected local adapter or proxy; disabling cached services alone may not clear a remote cache.
- If no command write has begun, the integration reconnects once, requests fresh service discovery, and retries the read. A second failure reaches the caller. Setup still rejects a device that lacks the required legacy protocol after fresh discovery.
- Once any command write has begun, the operation is not replayed, including when a write's result or the final feedback read is uncertain. Cache invalidation still prepares the next poll for fresh discovery. Device feedback, rather than the requested values, determines published state.
- Sessions and complete operations have bounded timeouts. Cleanup runs after success, failure, and cancellation. A client whose disconnect fails remains owned if it may still be connected, so cleanup can be retried before opening another connection. An already disconnected proxy client is released even if its disconnect RPC failed.

The coordinator continues scheduling polls after a transient failure while entities are subscribed. A successful later poll restores availability even when the reported control values are unchanged. Read-only setup verification and polling send no `SI01` or other control writes, so a plugged-in Rest with its light and sound off can be added and monitored without turning it on.

The recovery behavior has not been confirmed on the affected physical hardware. [VALIDATION.md](VALIDATION.md) records which tests have been executed and the remaining device checks.

## Update timing in 0.1.2

The inspected Android app subscribes to feedback notifications while connected. In `classes4.dex`, `NightlightDevice.java` (`Lcom/hatchbaby/rest/connectivity/ble/w3;`) selects the feedback characteristic in `x3` and subscribes through `l0`; callbacks reach `G2`. In `classes5.dex`, `RxBleConnectionImpl.java` (`Lar/t0;`) validates the NOTIFY property and calls `NotificationAndIndicationManager.java` (`Lar/q0;`) with the notification path. The app's two-second timer is a connection watchdog, not a feedback polling loop.

This integration instead opens short connections for each poll or command, freeing proxy connection capacity and allowing phone access between sessions. It does not maintain a persistent notification subscription.

The integration's default poll interval is 10 seconds and can be set per device from 5 to 300 seconds. A change made in the app or with a physical button becomes visible after the next poll connects and reads feedback. HA commands start without waiting for the poll timer, but can wait behind an existing operation on the same Rest. Each command transaction reads state, writes commands with the existing 0.5-second delay after each write, and reads final feedback.

In 0.1.2, successful command feedback is published immediately after that final read, before disconnect cleanup finishes. Only confirmed device values are published. The per-device operation lock remains held through cleanup, and the service call waits for cleanup before returning. Debug timing records lock waits, connection, feedback reads, and disconnection. These are integration scheduling choices; their effect on real Rest and proxy latency still needs hardware verification.

## Connection policy in 0.3.0

These are integration scheduling choices that supersede the 0.1.2 timing above; they need the same hardware verification.

- Default poll interval 60 seconds, configurable 15–300. Each poll is a full connection, discovery (cached), feedback read, and disconnect.
- HA's advertisement callbacks trigger an immediate refresh when a Rest that failed its last read advertises again, and mark entities unavailable when HA's Bluetooth manager reports the address unavailable. That report is ignored while this integration holds a connection, because a connected peripheral may stop advertising.
- Optional post-command hold (0–120 seconds, default 0). After a successful command the connection stays open; the per-device lock is released, and the next operation reuses the connection. While held, the integration subscribes to feedback notifications on `02260002-…`, matching the app's connected behavior, and publishes each valid packet. Command readback still uses an explicit feedback read after the 0.5-second settle, not a notification. Polls and program operations reuse a held connection without extending it; an idle timer disconnects when the hold expires. If a reused connection fails before any write, the operation reconnects once without clearing caches.
- Connection attempts per new connection: two for polls, three for commands and program operations, each with a 20-second connect timeout. Polls and commands are bounded at 60 seconds including lock wait; program operations keep a 165-second bound.
- A `disconnected_callback` fails an in-flight program request when the link drops.
- Program-table reads run each slot as its own locked step with a 5-second hold, then a separate `GT` step, so queued live operations run between slots on the same connection. Mutations remain a single locked session with a fresh baseline read.
- Hardware and firmware revisions are read once per service discovery; the firmware revision is attached to device state and the device registry.

## Native power and output controls since 0.1.5

The Rest exposes one shared power state. Media-player On/Off now controls that state directly; the separate Power switch is removed. Each action reads fresh device state before choosing its commands:

| Requested action | Device globally off | Device already on |
| --- | --- | --- |
| Media On | `SI01` only | No write |
| Media Off | No write | `SI00` only |
| Light on with nonzero brightness | `SI01`, then `SC` with requested RGB and brightness | `SC` only, preserving sound settings |
| Media Play or select a nonzero sound | `SI01`, then `SN` with selected sound | `SN` when needed, preserving lamp settings; explicit source selection can resend it |
| Light off or zero brightness | `SC` with zero brightness when needed; no wake | Same, preserving sound |
| Media Pause or select None | `SN00` when needed; no wake | `SN00` when needed, preserving lamp and shared power |

Ordinary power actions send no additional `SC`, `SN`, or `SV` commands. Restoration is left to firmware; no master settings snapshot is captured or persisted in HA. Light and sound wake sequences also omit opposite-output suppression and volume reapplication. `SI01` may therefore restore the other output: turning on the light may resume sound, and playing sound may resume the lamp. The integration does not substitute `SIFF`, which the inspected app uses for preset/program preview.

Version 0.1.3 introduced `SN00`/zero-brightness writes and volume reapplication to force isolated outputs after shared wake. Version 0.1.5 removes that behavior following reports of unexpected device behavior. The cause of those reports has not been established with a physical-device trace. These simpler command sequences follow the native shared-power behavior and still need hardware verification.

Light brightness resume and sound resume retain their last observed nonzero values in memory during the current entity session. Media Play can resume that sound; after a restart with no remembered sound it selects Pink noise. Media On uses only native power and does not choose a sound.

The media player reports `OFF` when shared power is off, `IDLE` when powered with sound ID zero, and `PLAYING` when powered with a nonzero sound ID. A selected nonzero track at zero volume retains its playing state. This keeps a powered, lamp-only Rest in `IDLE`, so the inherited power toggle chooses Off and sends `SI00` rather than turning audio on. Pause and source None remain the sound-only controls.

After successful integration setup, a narrowly matched entity-registry cleanup removes the obsolete Power switch while preserving Light/Sound entities. Automations that used the switch must use media-player On/Off for whole-device power, or Pause for sound-only silence.

The existing 0.5-second delay after each write remains. Confirmed final feedback is still published before disconnect cleanup, as described above; requested values are not published optimistically. The full HA suite and physical-device checks have not been executed locally. See [VALIDATION.md](VALIDATION.md) for executed test results and remaining checks.

## Native programs in 0.2.0

The following describes the shipped native-program implementation and its evidence. It does not establish firmware atomicity, rollback, timekeeping after power loss, or uninterrupted offline execution. [PROGRAMS.md](PROGRAMS.md) documents the HA workflow and [VALIDATION.md](VALIDATION.md) lists the remaining tests.

### App model and field encodings

In `classes4.dex`, `Luk/r0;` (`Program.java`) represents scheduled programs. `Luk/y;` (`Preset.java`) is the separate favorites model. `NightlightDevice.java`, `Lcom/hatchbaby/rest/connectivity/ble/w3;`, loads slots 1 through 10 in `Z2` and reloads one slot in `Y2`. Schedule records are sent to the BLE device; they are not a cloud-only routine representation.

| Field | Verified legacy representation |
| --- | --- |
| Slot | 1–10, encoded as two uppercase hexadecimal digits in slot-select/read commands |
| Start | Local `yyyyMMddHHmmss` text, with no timezone suffix |
| Duration | Seconds; classic `ESD` uses at least four hexadecimal digits. `DurationDialog.java` in `classes5.dex` exposes 0–17 hours and 0–59 minutes; a valid editor save requires a positive duration. The integration's full-write bounds are 60–64740 seconds. |
| Repeat days | `Luk/e;` (`Days.java`): Sunday `01`, Monday `02`, Tuesday `04`, Wednesday `08`, Thursday `10`, Friday `20`, Saturday `40`. Every day `7F`; an empty mask does not establish one-shot semantics. |
| Color | RGB followed by alpha/brightness, four raw bytes written as eight hexadecimal digits |
| Sound/volume | Built-in track ID and raw volume byte; the existing hardware-revision volume scaling applies |
| Classic flags | `Luk/g;` (`Flags.java`): exists `80`, enabled `40`. Other flag bits are retained when editing or toggling. |
| Name | The app layout limits names to 16 characters and the inspected binary record allocates 16 bytes. New/full integration writes require 1–16 printable ASCII bytes and exclude terminal response words; UTF-8 reads do not establish safe Unicode full-write encoding. |
| Buttons | The app's whole toddler-lock constructor produces `01FF` or `0000`. `ESM` adds a trailing `0000` argument. The editor/write gate is known numeric firmware version `10431` or newer. |

The app's `ProgramActivity`/`ProgramsActivity` and `DurationDialog` are the legacy editor path inspected in `classes5.dex`. No fade or multistep field was established there. Existing raw duration seconds are retained when HA's minute-based duration inputs remain unchanged.

### Classic reads and acknowledged transactions

The integration uses classic field reads for all ten slots. It reads `EGL{slot}` first; absent slots need no invented control values. Occupied slots require the following replies, with `EGM` conditional on known firmware support:

| Command prefix | Field |
| --- | --- |
| `EGL` | Classic flags |
| `EGC` | RGB and brightness |
| `EGD` | Duration seconds |
| `EGI` | Opaque power/mode byte |
| `EGM` | Button mask |
| `EGN` | Sound ID |
| `EGP` | Opaque presets value |
| `EGT` | Local start date/time |
| `EGV` | Raw volume |
| `EGX` | Name |
| `EGW` | Weekday mask |

Each read command appends the two-digit slot. `Program.java` methods `f0`, `g`, and `l` establish this classic read path. The app also attempts `EGB` binary reads in `e0`/`h0` before its classic fallback; 0.2.0 does not depend on that binary decoder. Binary presence/option bytes are distinct from classic `EGL`/`ESL` flags and must not be interchanged.

Full writes follow the app's `t0` → `n` sequence:

```text
ESB{slot}
EST{local yyyyMMddHHmmss}
ESD{duration seconds in hex}
ESW{weekday mask}
ESI{preserved power/mode}
ESC{RGBA}
ESN{sound}
ESV{volume}
ESX{name}
ESM{button mask}0000       [only with known firmware support]
ESL{classic flags}
ESF
```

`EGP` is read and retained for verification, but the inspected writer does not set a corresponding `ESP` field; the integration does not invent one. A toggle uses `ESB{slot}` → `ESL{flags with enabled bit changed}` → `ESF`. Delete uses `ESB{slot}` → `ESL00` → `ESF`. `ESF` has a finish/commit role in the observed app sequence; its exact firmware atomicity and failure behavior remain unverified.

Program commands use the existing command TX UUID `02240002-5efd-47eb-9c1a-de53f7a2b232`. Replies arrive through notifications on command RX `02240003-5efd-47eb-9c1a-de53f7a2b232`, separately from live feedback. `NightlightDevice.D3`, `N2`, and the inner request wrapper `w3$d` establish notification subscription, fragment accumulation, and terminal replies. Data notifications are concatenated for the in-flight command until explicit `OK`; `E01`–`E06` and `Unknown` are errors. A completed GATT write alone is not application acceptance. The integration uses bounded response buffers, command/session timeouts, and one serialized request stream.

HA save/delete/enable operations first reread the complete affected slot and compare it with a baseline. The editor uses its immutable selected record, and program switches use the cached record. A changed slot, reused slot, or newly occupied creation slot yields a conflict before writing; enable checks also happen before accepting a no-op. A matched toggle changes only the enabled flag. Editor conflicts return to the form; switch conflicts surface as HA action errors, and the program table is unavailable until a successful refresh. Successful mutations require post-commit readback matching the expected result. Once an `ESB` mutation starts, failure never triggers automatic replay; a partial write may remain and the user must refresh and inspect it. Preflight comparison is not an atomic compare-and-swap across multiple clients.

### HA state and device time

Programs load after setup, hourly (every 15 minutes before 0.3.0), and on editor/explicit refresh. This table uses the same BLE client and lock as live controls; since 0.3.0 live operations can run between slot reads. HA stores no separate program options or persistent schedule; occupied slot switches expose native enabled state using stable `<MAC>:program:<slot>` unique IDs. Their enabled state does not mean a program is currently running. Deletion removes only the corresponding slot entity.

`GT` reads device clock text through `NightlightDevice.I1`/`J3` and local `DateUtils` parsing. The app has a separate `ST` clock-write path, with a firmware-gated `U`/`E`/`N` rule suffix; 0.2.0 does not issue `ST` or infer a full timezone from those rules. Clock data is informational. Program wall times are naive local values, and a start-plus-duration summary is not a fixed wall-clock end through DST.

Hatch's [program guidance](https://help.hatch.co/hc/en-us/articles/115001825327-Setting-Programs) establishes next-run edit behavior. Its [classic Rest DST guidance](https://help.hatch.co/hc/en-us/articles/115003020554-Daylight-Savings-and-Your-Hatch-Devices) supplies overlap and duration context plus the app's Sync Time procedure. Those product descriptions supplement app inspection; real-device acceptance remains pending.

The UI uses HA's [options flow](https://developers.home-assistant.io/docs/core/integration/options_flow/) menus/forms. It does not create HA Schedule helpers, calendar entities, fades, multistep routines, or monthly/exception rules. Device-resident execution is an architectural inference until verified with HA and the proxy offline. Research notes are summarized here without distributing the APK, DEX files, app code, or extracted assets.

## Discovery

The legacy app parses raw advertising records in `classes4.dex`, `Luk/d;`, source `Data.java`, `k(boolean)`. Its legacy manufacturer signature includes bytes `34 04` followed by ASCII `R` or `r`, corresponding to Bluetooth manufacturer ID `0x0434` (1076), with the first manufacturer payload byte `R`/`r`. The application also recognizes an older signature `13 37 R/r`. HA/Bleak's little-endian manufacturer-data key for those alternate bytes is `0x3713` (14099); version 0.1.1 matches that key as well. This is an encoding of the inspected legacy signature, not a claim about a currently registered manufacturer assignment.

The app also recognizes Battery Service service-data advertising (`0x180f`), and recovery names `RestDFU` and `NOSDDFU`. Those are not sufficient first-generation discriminators. Discovery should narrow to the legacy manufacturer signature and confirm the legacy TX and feedback GATT characteristics before setting up an entry. A user-set local name alone does not establish device generation.

The manual address fallback allows selection when a device is absent from the manufacturer-filtered chooser. It requires HA to have observed the address through a connectable adapter or active proxy and uses the same HA-selected route and GATT verification as discovery. It does not require the lamp or audio to be on; the device must remain plugged in and available for a BLE connection.

## ASCII commands

Commands are ASCII characters, with uppercase hexadecimal parameters; they are not binary hex byte strings and have no newline terminator. `classes4.dex`, `Luk/c;`, source `Commands.java`, `a`/`b` use `%02X` and `%s%02X`. `NightlightDevice.w3.O0` passes the command's string bytes to the characteristic write.

| Command | Meaning / encoding | Android evidence |
| --- | --- | --- |
| `SI00` | Power off | `classes5.dex`, `RemoteActivity.onPowerBtnClicked`, branch logged as "Powering OFF pressed" |
| `SI01` | Power on | Same method, branch logged as "Powering ON pressed" |
| `SIFF` | App's preset/program preview mode | `PresetActivity.V`, `ProgramActivity.f0`; not used as an ordinary power-on command |
| `SCrrggbbaa` | Set RGB and brightness alpha, each raw byte 0-255 | `RemoteActivity.j0`, `Luk/b;` source `Color.java`, `i` |
| `SYaa` | Set brightness only, raw byte 0-255 | `RemoteActivity.E0` |
| `SNnn` | Set sound/track ID | `RemoteActivity.u0` |
| `SVvv` | Set volume raw byte | `RemoteActivity.H0` |

The ordinary power button sends `SI01` independently. The preview path constructs `SC`, `SV`, `SN`, then `SIFF`; the restoration path constructs `SI` before restoring `SC`, `SN`, `SV`, and button behavior. The app does not establish that `SI01` after an arbitrary control write preserves that write. Sending `SI01` before requested `SC`/`SN` values when starting an off device is a conservative implementation inference, pending hardware confirmation.

Brightness is 0-255 on the wire. Ordinary legacy REST volume also spans 0-255, with a UI slider of 0-100. The app's `HBSeekBar.R` / `S` in `classes5.dex` convert between these scales. For BLE hardware revision strings starting with `2`, the app maps UI percent to `round(percent * 247 / 100) + 8`, and inversely from the 8-255 raw range. The `classes4.dex`, `Lfl/a;`, source `RestV2BLEDevice.kt`, `a` helper calls `String.startsWith("2")` through the Kotlin string façade. This is a revision of the app's BLE `REST` path; the filename alone does not imply a Wi-Fi second-generation device.

RGB `FE FE FE` is the app's rainbow mode. `classes5.dex`, `LightColorChooserView.e` maps resource ID `2131428448` (`id/rainbow_btn`) to ARGB `0xFFFEFEFE`; `Color.java` special-cases that color for the rainbow icon.

## Feedback fields

`classes4.dex`, `Luk/d;`, source `Data.java`, `k(boolean)` walks a tagged byte stream. `j(byte)` gives each tag's total field length. The application does not rely on fixed field offsets or one specific ordering.

| Tag | Total bytes including tag | Payload |
| --- | --- | --- |
| `T` | 5 | Four-byte big-endian seconds; application converts to milliseconds |
| `C` | 5 | Red, green, blue, brightness alpha |
| `S` | 3 | Sound ID, unsigned volume raw byte |
| `P` | 2 | Preset/power flags |
| `e` | 2 | Active event index in low seven bits; timer active in bit `0x80` |
| `E` | 6 | Next-event information, not used for controls |

The exact power formula in the app is:

```python
power_on = (preset_power_flags & 0xC0) != 0xC0
```

This matters: `0x00`, `0x40`, and `0x80` report on; `0xC0` reports off. It is not the truth value of bit 0, or of the entire flags byte. Independently verified Dalvik instructions in `Data.k` mask by 192, compare for equality to 192, write false on equality, and true otherwise. `Data.h()` returns that field; `RemoteActivity.I0` uses it to select the power button. The parser must reject truncated known fields and handle missing required control fields without publishing invented state.

## Built-in sound IDs

Verified from `classes4.dex`, `Luk/s0;`, source `Track.java`, `<clinit>`, with the referenced string IDs resolved from the APK's `resources.arsc`:

| ID | App label |
| --- | --- |
| 0 | None |
| 2 | Stream |
| 3 | Pink Noise |
| 4 | Dryer |
| 5 | Ocean |
| 6 | Wind |
| 7 | Rain |
| 9 | Bird |
| 10 | Crickets |
| 11 | Brahms |
| 13 | Twinkle |
| 14 | Rockabye |

The reference integration calls ID 3 White Noise; this APK labels its legacy track Pink Noise. IDs 1, 8, and 12 are not in this app's legacy display-name map.
