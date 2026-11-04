# Hatch Rest BLE — first generation

![Hatch Rest BLE icon](custom_components/hatch_rest_ble/brand/icon.png)

A local Home Assistant custom integration for the original Bluetooth-only Hatch Rest. It uses Home Assistant's shared Bluetooth stack, including connectable local adapters and active Bluetooth proxies. No Hatch account or cloud connection is needed.

Development baseline: **Home Assistant 2026.10**. Version **0.4.0** shows each of the Rest's native programs as its own device, with a control for every setting. Version 0.3.0 reduced Bluetooth traffic: a 60-second default poll, immediate refresh when an unreachable Rest starts advertising again, an optional connection hold after commands, and program-table reads that no longer block live controls. Version 0.2.0 added management of the Rest's ten native program slots (**Configure → Manage programs**).

**415 tests pass** (240 transport/protocol unit tests and 175 Home Assistant tests against HA 2026.10's real config-entry, coordinator, entity, and Bluetooth classes), with Ruff lint and formatting clean. Physical-device verification remains pending. See [VALIDATION.md](docs/VALIDATION.md) for test scope and remaining checks. Earlier HA releases have not been tested.

## Install

**Home Assistant Marketplace or HACS:** search for **Hatch Rest BLE (1st Gen)** and install it. Until the catalog listing is approved, add `https://github.com/FutureGUIs/hatch-rest-ble` as a custom repository of type *Integration* in HACS.

**Manual:** copy `custom_components/hatch_rest_ble` into your Home Assistant configuration directory's `custom_components` folder and restart Home Assistant.

Then:

1. Make sure HA's Bluetooth integration is enabled, with an adapter or active proxy capable of making connections.
2. Look for **Hatch Rest BLE (1st Gen)** under **Settings → Devices & services → Discovered**, or choose **Add integration → Hatch Rest BLE (1st Gen)**.
3. Select and confirm the device. If it is missing from the list, enter its complete Bluetooth MAC address in the manual field; fill in only one field. HA must already have seen that address through a connectable adapter or active proxy. Close the Hatch app so it releases its BLE connection. Setup verifies the first-generation GATT characteristics and reads valid state before creating an entry.

Each nearby Rest can be added separately, including a second Rest entered by address if it is missing from the chooser. The integration discovers the legacy `R`/`r` advertisements under manufacturer IDs 1076 and 14099 through HA. Bluetooth range includes the reach of active proxies. Rest+, Rest Mini, Restore, and the Wi-Fi second-generation Rest are excluded. A later hardware revision of the original BLE Rest remains supported, including its different volume scale.

The Rest must be plugged in, but its light and sound can be off. Discovery, setup verification, and polling do not send power-on commands.

The domain is `hatch_rest_ble`. If you use another integration for the same device, disable that device's old entry before testing to avoid competing BLE connections. Moving from a different integration does not migrate its entities or automations. Source repository: [FutureGUIs/hatch-rest-ble](https://github.com/FutureGUIs/hatch-rest-ble).

## Upgrade from 0.3.x

Install 0.4.0 and restart. Each existing program becomes a **Rest program** device under the Rest after the first program read. The program's enable switch keeps its entity ID and history but moves to that device and is now named **Enabled** (its friendly name becomes "<Rest> program <slot> Enabled"). Automations that use its entity ID keep working.

## Upgrade from 0.2.x

Install 0.3.0 through the Marketplace/HACS or replace the `custom_components/hatch_rest_ble` directory, then restart. Entries and entity IDs are retained. Behavior changes:

- Entries without a saved polling interval now poll every **60 seconds** instead of 10. Saved intervals below the new 15-second minimum run at 15 seconds. App and button changes still appear on the next poll, or immediately while a connection is held (below).
- The program table refreshes hourly instead of every 15 minutes. Edits made in HA, the **Refresh programs** button, and the editor's reload still read immediately.
- A new, disabled-by-default **Signal strength** diagnostic sensor is added to each Rest.

## Upgrade from 0.1.x

Replace the existing `custom_components/hatch_rest_ble` directory with the directory from the 0.2.0 release, including its `brand` folder, then restart Home Assistant. Existing device entries and Light/Sound entity IDs are retained; the devices do not need to be removed and added again. Programs are read from the Rest after setup; upgrading alone does not create or change one. After successful setup, the integration removes only its obsolete whole-device Power switch entry from the entity registry. Update automations that used that switch to use `media_player.turn_on` or `media_player.turn_off` on the Sound entity for whole-device power. Use `media_player.media_pause` for sound-only silence. Changes are listed in [CHANGELOG.md](CHANGELOG.md).

The approved Hatch logo is bundled in `brand/icon.png` (256×256) and `brand/icon@2x.png` (512×512), following [HA's local branding support](https://developers.home-assistant.io/docs/core/integration/brand_images/). See [branding attribution](docs/BRANDING.md) for its official source.

## Controls

| Entity | Behavior |
| --- | --- |
| Light | RGB, brightness, and the built-in Color loop/Rainbow effect. Light on from whole-device off sends `SI01` followed by the requested color and brightness. Light off sets brightness to zero while retaining sound. |
| Sound media player | On/Off controls whole-device power, including light and sound. Play resumes sound; Pause or source None silences sound while retaining the lamp. Select built-in sounds and set volume. |
| Program devices | One device per stored program; see [Native programs](#native-programs). |
| Add program button | Creates a program in the first free slot (8 PM, 1 hour, every day, with the Rest's current light and sound), which appears as a new program device. |
| Refresh programs button | Reads the Rest's program table again, including changes made in the Hatch app. |
| Signal strength (diagnostic, disabled by default) | RSSI of the Rest's latest advertisement, with the receiving adapter or proxy in its `source` attribute. It keeps updating while polls fail, which helps diagnose range and proxy routing. |

Media-player On/Off reads fresh state and sends only the ordinary `SI01` or `SI00` command when needed. The Rest's firmware handles its remembered settings. The integration does not capture a master settings snapshot or add color, sound, or volume writes to a power action.

The Sound entity reports **Off** when whole-device power is off, **Idle** when power is on with source None, and **Playing** when power is on with a selected sound. Its power toggle therefore turns off a Rest that has only its lamp active. Pause and source None send only `SN00` when a change is needed; they do not turn off the lamp. Play resumes the last observed nonzero sound, or selects Pink noise after a restart with no previous sound. Light brightness and sound resume choices are remembered during the current entity session.

The Rest has one shared hardware power command. Light on from off sends `SI01` then the requested `SC`; Play or a nonzero source from off sends `SI01` then the selected `SN`. These actions no longer force the other output off or reapply volume. Waking the light may resume sound, and waking sound may resume the lamp, according to the device's native behavior. When already powered on, changing one output leaves the other output's settings alone. The integration does not use the app's `SIFF` preview mode for these controls.

Available sounds: None, Stream, Pink noise, Dryer, Ocean, Wind, Rain, Birds, Crickets, Brahms' lullaby, Twinkle twinkle, and Rock-a-bye. Sound ID 3 is called White noise in the reference integration and Pink noise in the supplied Android app. The device plays its built-in tracks; it cannot stream media URLs.

Changes to current light and sound made in the app or with the Rest's buttons are picked up on the next live poll, plus the time needed to connect and read feedback. The default interval is 60 seconds. Every poll is a full Bluetooth connection, and on an ESP32 proxy each connection takes a connection slot and interrupts scanning while it is set up, so frequent polling of several devices degrades every other Bluetooth device on that proxy. To change the interval for one Rest, open its integration entry under **Settings → Devices & services**, choose **Configure → Updates and connection**, and set 15 to 300 seconds. Saving reloads that entry.

The integration also follows the Rest's advertisements through HA. When a Rest that could not be reached starts advertising again, it is read immediately instead of waiting for the next poll. When HA stops hearing it, its entities become unavailable without a connection attempt. With debug logging enabled, changes in its advertisement data are logged; those logs will show whether the Rest's advertisements carry its state, which would allow updates without polling.

**Keep connected after a command** (same options page, 0–120 seconds, default 0) holds the connection open after an HA command instead of disconnecting. Follow-up commands, such as several brightness changes, then skip the connection setup, and button presses on the Rest are pushed to HA immediately through feedback notifications while the connection is open. Polls reuse a held connection but never extend it. The trade-off: while it is held, the Hatch app cannot connect, and the proxy's connection slot stays in use. A held connection that has gone stale reconnects once if nothing was written yet.

HA commands do not wait for the poll timer, but can wait behind another operation on the same Rest. They read fresh state, apply related writes in one serialized transaction, and read device feedback. Successful commands publish that confirmed feedback before waiting for disconnection, so proxy cleanup does not delay the entity update. The service call still waits for cleanup to finish. Each new connection resolves HA's current Bluetooth route. A poll makes at most two connection attempts, a command three. Including any wait behind another operation on the same Rest, a poll or command fails within 60 seconds rather than hanging. Failed reads or writes make entities unavailable and failed actions raise an error. Polling or a returning advertisement can recover availability.

If a read encounters an invalid GATT handle, missing characteristics, or malformed feedback, the integration clears the cache on the connected Bluetooth client, disconnects, and retries once with fresh services through HA's current route. Once a command write has begun, it reports failure without replaying the commands; a later poll can refresh state and restore availability.

Sessions release their connection on completion, error, cancellation, unload, or HA shutdown, unless a hold is active, in which case an idle timer releases it. A failed disconnect is retained for another cleanup attempt if the client may still be connected; an already disconnected proxy client is released. If the Rest drops the link mid-request, the request fails at once instead of waiting for its timeout. Connection attempts and operations have bounded timeouts. The conservative 0.5-second pause after each write needs verification on hardware. By default sessions are short, which frees proxy connection capacity and lets the phone app use the device between polls.

## Native programs

The Rest stores up to **10** programs. Each one appears in HA as its own device, **<Rest> program <slot>** (for example *Nursery Rest program 3*), listed under the Rest. The number is the program's slot on the Rest, so it shows how many of the 10 are in use. Program devices share the Rest's area unless you give them their own, and are disabled and removed with the Rest.

Each program device has:

| Entity | Type | Notes |
| --- | --- | --- |
| Enabled | Switch | Arms the program for future starts. It does not show whether it is running now. |
| Monday … Sunday | Switch | Repeat days. With none on, the program never repeats. |
| Name | Text | 1–16 printable ASCII characters, as in the Hatch app. |
| Start time | Time | On the Rest's own clock. |
| Duration | Number (minutes) | 1 minute to 17 h 59 min. |
| Color | Text | `#RRGGBB`, for example `#FF8000`. |
| Brightness, Volume | Number (%) | Scaled like the Hatch app. |
| Sound | Select | |
| Button lock | Switch | Only on firmware 1.0.4.31 or newer. |

Changes are written to the Rest and only shown once the Rest confirms them. Changes to one program made within about a second, such as switching several days on or off, are written together as one save. A displayed value you don't change keeps its exact device value, so editing the name doesn't round the volume or drop a 30-second duration offset.

- **Add** a program with the Rest's **Add program** button. It uses the first free slot; when all 10 are in use, delete one first.
- **Delete** a program by deleting its device from the program device's page. The program is deleted from the Rest first; if that fails, the device stays and HA shows why.
- Programs added or deleted in the Hatch app appear or disappear after the next program read (hourly, or **Refresh programs**). A failed read never removes anything.

**Configure → Manage programs** remains available as a single form for editing a whole program at once. See [PROGRAMS.md](docs/PROGRAMS.md) for fields, refresh behavior, conflicts, and clock details.

The program table is read in the background after setup, hourly, and when explicitly refreshed through the editor or button. It is separate from live light/sound polling. A table read is about a hundred acknowledged requests, so it reads one slot at a time on a shared connection and lets queued polls and commands run between slots instead of after the whole table. Programs remain on the Rest as the source of truth; they are not saved as HA schedules or replayed from HA storage. The device protocol strongly suggests firmware execution, but continued operation with HA and the proxy offline still requires a hardware check.

Before the first program write, record existing settings in the Hatch app and test with an unused slot. A failed transaction may have changed part of a slot; refresh and inspect both HA and the app before retrying. All program changes compare a fresh complete record with the previously displayed record. A conflict from a program device entity reloads the program table and asks you to make the change again; an editor conflict stays on the form. The integration requires device acknowledgements and saved-state readback, and never automatically replays a program mutation. Its clock read is informational; it does not set device time or apply DST changes. The [program guide](docs/PROGRAMS.md) explains when to use the app's Sync Time control.

## Example actions

Replace these example entity IDs with the IDs assigned by your HA instance:

```yaml
action: light.turn_on
target:
  entity_id: light.nursery_rest_light
data:
  rgb_color: [255, 100, 30]
  brightness: 100
```

```yaml
action: media_player.select_source
target:
  entity_id: media_player.nursery_rest_sound
data:
  source: Rain
```

## Troubleshooting and device verification

If setup cannot connect, check HA's Bluetooth configuration, enable active connections on your proxy, move it closer, close the phone app, and retry. Passive-only receivers can detect advertisements but cannot control the Rest. A busy device may stop advertising while the phone is connected. Use HA's integration debug logging to capture connection failures; the integration never scans separately or opens raw address-only Bleak connections.

For delayed commands or updates, enable debug logging on the integration entry and reproduce one change. Timing messages distinguish waiting for another operation on the same Rest, connecting, reading feedback, and disconnecting. A command cannot update HA until its feedback has been read, and the unchanged 0.5-second delay after each write also contributes to command time. Keep the phone app closed during this check so its connection does not compete with HA.

If a plugged-in Rest is visible in the Hatch app but missing from HA's chooser, use the manual address field after confirming that HA can see it through a connectable Bluetooth source. Keeping its light and sound off is supported. If `Invalid handle` persists after upgrading, capture the integration's debug log through a failed poll and a later poll; cache recovery still needs verification with the affected adapter or proxy.

For an ESPHome proxy, check for a backend warning that on-device cache clearing is unavailable and update the firmware installed on the proxy if that warning appears. The [ESPHome backend](https://github.com/Bluetooth-Devices/bleak-esphome/blob/v4.0.0/src/bleak_esphome/backend/client.py) can only clear the proxy's stored cache when its firmware advertises that capability. ESP32 proxy service caches are stored in flash, so restarting the proxy may leave stale services intact. As a diagnostic workaround for persistent invalid handles on ESP32, merge `cache_services: false` into the existing `bluetooth_proxy` section and install the changed configuration; this disables that cache and makes connections slower. See [ESPHome's proxy configuration](https://esphome.io/components/bluetooth_proxy/). This is a proxy setting, not an integration option.

Before treating this release as production-ready, run the device checks in [VALIDATION.md](docs/VALIDATION.md). In particular, compare power, color, sound, and zero/maximum volume with the app; test the physical buttons, out-of-range recovery, proxy routing, reload, and shutdown. The app inspection establishes the protocol, but does not establish which firmware variants need a longer write delay or whether every proxy can connect reliably.

## Development

The integration code is in `custom_components/hatch_rest_ble`. Pure protocol and transport tests run without installing Home Assistant:

```sh
python -m pip install -r requirements-unit.txt
python -m pytest tests/unit -q
```

On Linux with Python 3.14, run the tests against the real HA classes and lifecycle:

```sh
python -m pip install -r requirements-test.txt
python -m pytest tests/ha -q
python -m ruff check .
python -m ruff format --check .
```

The GitHub Actions workflow runs both suites separately. Unit tests load the protocol and transport modules under an isolated package name so importing the integration entry point does not require HA. Device I/O is mocked; these tests do not replace hardware verification.

## Research and attribution

Based on protocol knowledge from [jcgoette/hatch_rest_homeassistant](https://github.com/jcgoette/hatch_rest_homeassistant), commit `ccf87c6c26c7eb0f4ab61ba59dc2e7fa0a8a8eec`, the original [pyhatchbabyrest](https://github.com/kjoconnor/pyhatchbabyrest), and analysis of the user-supplied Hatch Sleep 6.40.1 XAPK. [PROTOCOL.md](docs/PROTOCOL.md) records APK hashes, method references, verified facts, and inferences. The APK and decompiled app code are not distributed.

HA routing follows the official [Bluetooth practices](https://developers.home-assistant.io/docs/bluetooth/) and [Bluetooth APIs](https://developers.home-assistant.io/docs/core/bluetooth/api/). The implementation uses [bleak-retry-connector](https://github.com/Bluetooth-Devices/bleak-retry-connector) from HA's Bluetooth dependencies. MIT notices for the reference work are retained in [LICENSE](LICENSE).
