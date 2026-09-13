# CUL868 Firmware Flasher

An experimental Home Assistant add-on for deliberately flashing a user-supplied
Intel HEX firmware image to a **CUL868 V3** USB radio. It is intentionally not
an updater: it does not discover releases, download firmware, build firmware,
or flash without confirmation.

## Scope and target identification

The add-on supports only a CUL868 V3, whose normal application is built for an
ATmega32U4. It validates the normal serial endpoint before asking it to enter
DFU, and validates the uploaded Intel HEX file before it can be queued.

| USB state | VID:PID | Expected characteristic | Add-on handling |
| --- | --- | --- | --- |
| CULFW, a-culfw, or TSCUL normal CUL868 application | `03eb:204b` | CDC serial device; firmware answers `V` with `CUL868` | Required configured device |
| CUL868 V3 bootloader | `03eb:2ff4` | ATmega32U4-compatible DFU | Automatic recovery only on a saved path; otherwise one explicitly confirmed unique target |
| Older CUL bootloader | `03eb:2ffa` | Different legacy CUL target | Intentionally unsupported |

The normal `03eb:204b` descriptor is shared by the CUL firmware families
above. The serial `V` response is therefore the final identity check before
the add-on sends `B01` to reboot into DFU.

## Install and use

1. Add this repository to Home Assistant's add-on store and install
   **CUL868 Firmware Flasher**.
2. In the add-on configuration, enter the stable normal CUL868 serial path,
   such as `/dev/serial/by-id/...`. Do not choose a bootloader device. Keep
   this saved path unchanged if the normal TTY disappears during DFU recovery.
3. Start the add-on and open its Ingress page.
   At startup it briefly pauses a matching wmbusmeters instance to read and
   record the CUL's `V` version response, then restores wmbusmeters. An
   unavailable CUL or failed read is reported in the add-on log but does not
   prevent the flasher from starting.
4. Choose a firmware `.hex` file, then select **Validate firmware**.
5. Review the SHA-256, application address range, and USB preflight result.
6. Tick the explicit overwrite confirmation and select **Flash CUL868 V3**.
   If the UI identifies an **unpaired CUL DFU bootloader**, verify its physical
   USB path and tick the additional recovery confirmation. This is required
   only when the add-on has no prior verified identity for the radio.
7. Wait for a verified `V` response before using the radio again.

The add-on accepts only bounded ASCII Intel HEX input with valid checksums,
one EOF record, non-overlapping application records, and an application reset
vector at address `0x0000`. It rejects writes from `0x7000` onward. CUL V3
uses the top 2 KiB for its bootloader, while upstream `dfu-programmer` reserves
the top 4 KiB for its generic ATmega32U4 target; the stricter boundary makes
validation match the tool that will actually flash the device. This guards the
DFU input format; it cannot prove that an operator-selected firmware image is
functionally correct.

## Recovery

After the add-on has successfully verified the normal CUL application at least
once, it stores only the physical USB topology, configured serial path, and
non-secret identity data. If a failed flash leaves that same radio in
`03eb:2ff4` DFU mode, the add-on can still start even though the normal TTY no
longer exists. The UI can then perform recovery only when the configuration
still names that exact saved serial path. It will never choose an arbitrary DFU
device merely because it is the only bootloader currently visible, or reuse
recovery state after the selected CUL device changes.

The Supervisor configuration intentionally uses a plain string rather than a
`device(subsystem=tty)` selector: the latter rejects add-on startup while the
normal TTY is absent. The app still validates the value as a bounded `/dev/...`
path and requires the saved physical USB topology before it can touch DFU.

As soon as the add-on requests `B01`, the prior firmware version is marked
unknown. The add-on keeps the verified topology for recovery, but will not keep
showing a stale firmware version after a failed or incomplete write.

If the radio has never been verified by this add-on and is already in DFU mode,
the UI can offer one-time recovery only when exactly one `03eb:2ff4` CUL868
bootloader is visible. It requires a separate confirmation that this physical
device is the configured CUL868. The server, not the browser, retains that
selected USB topology and rechecks it immediately before each DFU command.
The new binding is persisted only immediately before the authorized erase.

If no expected bootloader is visible, or more than one is visible, recovery is
refused. Reattach or detach devices until the intended bootloader is the only
candidate; the add-on will never choose one arbitrarily.

## wmbusmeters coordination

Before a startup version read or a flash, the add-on reads only the `device`
setting from each recognized running wmbusmeters add-on's Supervisor-provided
options. It pauses and restores exactly the instances configured with the
selected CUL path, including an equivalent resolved `/dev` path. It also pauses
`auto` and `cul` discovery modes because they can probe the selected serial
radio. Other wmbusmeters instances remain running. The implementation neither
logs nor stores the options, which may contain MQTT credentials.

A startup version check or a flash that fails before DFU begins restores those
instances. Once the add-on requests `B01`, or begins bootloader-only recovery,
it leaves matching wmbusmeters instances stopped if DFU or final `V`
verification fails. This prevents a meter reader from reopening a radio with
unknown firmware. They are restored automatically only after the CUL application
has returned and answered `V`; after a failed update, repair the radio first and
then start wmbusmeters manually.

## Virtual machines and USB re-enumeration

The CUL changes USB identity between `03eb:204b` and `03eb:2ff4`. Your
hypervisor must make both personalities available to Home Assistant. A
VID:PID-only assignment can lose the device at the personality change; mapping
a physical USB port or USB controller is generally more reliable. QEMU's own
USB documentation describes `hostbus` and `hostport` as the physical-port
selection method and notes host USB replug limitations. See the
[QEMU USB documentation](https://www.qemu.org/docs/master/system/devices/usb).

Enable **QEMU USB re-enumeration workaround** if Home Assistant sees the DFU
bootloader but QEMU places it on a different *guest* USB path after `B01`. It
adds an 8-second settle delay before the first post-transition USB probe and,
only after a verified CUL has received `B01`, permits one expected DFU target
to move to another guest path. The same exact-one check applies when the CUL
application returns. Multiple candidates or a descriptor-serial mismatch are
always rejected.

The active transition wait is never shorter than 90 seconds and can be
increased to 120 seconds with **Boot timeout**. It covers both the application
USB node and its final `V ... CUL868` response: a returned CDC node alone is
not accepted as a successful flash. This accommodates firmware such as
a-culfw which can restart again while initializing persistent state after a
firmware transition. This cannot repair a passthrough mapping that removes the
device from the guest: both personalities must still be visible to Home
Assistant.

## Security model

- Home Assistant Ingress is the only UI. The service accepts requests only
  from the Home Assistant Ingress proxy and requires the Ingress request header
  for state-changing calls.
- Uploads are size-bounded, stored with mode `0600` on add-on tmpfs, expire
  after 15 minutes, and are deleted after an attempted flash.
- Only one flash can run or queue at a time. A process lock is a second guard
  around serial and raw USB access.
- `dfu-programmer` receives an exact `atmega32u4:<bus>,<address>` selector
  resolved from the validated physical USB topology. It is invoked without a
  shell and without Supervisor credentials in its environment.
- A bootloader with no retained identity needs a second one-time Ingress
  confirmation. Its server-side USB topology and descriptor serial are rechecked
  before use; multiple visible bootloaders are rejected.
- The AppArmor child profile for `dfu-programmer` has raw USB access but no
  network permission. The Python process has no raw USB device-node access.

The add-on requests Home Assistant's `manager` role only because the Supervisor
requires it to read an add-on's device option and to stop/start a matching
wmbusmeters app around a flash. The role grants broader Supervisor authority
than this implementation uses, so install it only from a trusted repository.
The relevant option and lifecycle endpoints are documented in the [Home
Assistant Supervisor API](https://developers.home-assistant.io/docs/api/supervisor/endpoints/).

The standard CUL DFU sequence requires `erase`, `flash`, then `start`, matching
the upstream CUL V3 flash script. The add-on never submits bootloader-address
data to `dfu-programmer`, but it cannot verify the lock-fuse state of a
nonstandard or damaged bootloader. Use this only with the expected stock
`03eb:2ff4` CUL V3 DFU bootloader.

## Licensing

This add-on's Python and web code is Apache-2.0. It builds and invokes the
unmodified GPL-2.0 `dfu-programmer` v1.1.0 as a separate executable; its full
corresponding source and `COPYING` file are included in the image at
`/usr/src/dfu-programmer`. `dfu-programmer` dynamically uses Alpine's
`libusb` package (LGPL-2.1-or-later); its license text is included in the
image alongside checksum-verified corresponding source and the runtime package
inventory. The complete runtime component inventory is in
[`cul868_flasher/LICENSES/COMPONENTS.md`](cul868_flasher/LICENSES/COMPONENTS.md).

CULFW, a-culfw, TSCUL, LUFA, and wmbusmeters are not copied into this add-on.
Their licensing remains relevant to firmware you independently obtain and
choose to upload. Project and vendor names identify compatibility only and do
not imply endorsement. This record is not legal advice.
