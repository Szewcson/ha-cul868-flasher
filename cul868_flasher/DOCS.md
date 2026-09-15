# CUL868 Firmware Flasher

This experimental Home Assistant app deliberately flashes a user-supplied Intel
HEX image to one **CUL868 V3** USB radio. It is not an updater: it does not
discover releases, download firmware, build firmware, or flash without an
explicit confirmation.

## Scope

| USB state | VID:PID | Required evidence |
| --- | --- | --- |
| CULFW/CULFW1, a-culfw, or TSCULFW application | `03eb:204b` | CDC serial endpoint and a `V ... CUL868` or `VTS ... CUL868` response |
| CUL868 V3 bootloader | `03eb:2ff4` | Atmel-compatible DFU bootloader on a verified or explicitly confirmed USB path |
| Older CUL bootloader | `03eb:2ffa` | Unsupported |

The shared normal USB descriptor is not enough to identify a CUL firmware
family. Before sending `B01`, the app opens the configured endpoint and checks
the firmware's `V`/`VTS` response. It accepts only bounded ASCII Intel HEX
input with valid checksums, one EOF record, non-overlapping application records,
and a reset vector at `0x0000`. Writes at `0x7000` and above are rejected.

## Configure

- Set **Device** to the normal application serial endpoint, preferably a direct
  `/dev/serial/by-id/...` path. Do not configure a bootloader endpoint.
- Select the baud rate used by the installed CUL firmware. `9600` is the usual
  default; use the firmware's documented value if it differs.
- **Boot timeout** is the maximum normal wait; post-DFU verification always
  waits at least 90 seconds and can wait up to 120 seconds.
- Enable **QEMU USB re-enumeration workaround** only when QEMU moves the same
  radio to a different guest USB topology after the `B01` personality change.
- Use **Additional CUL consumer apps** only for a known app such as FHEM or
  Homegear whose CUL path cannot be inspected safely by this app.

The app uses a plain string instead of Home Assistant's `device(subsystem=tty)`
schema because the normal TTY is intentionally absent while the radio is in
DFU mode. The value is still constrained to a safe `/dev/...` path.

## Flash A Firmware File

1. Start the app and open its Ingress page. Startup attempts a non-destructive
   version read, temporarily pausing matching CUL consumers.
2. Choose a trusted `.hex` file and select **Validate firmware**.
3. Review the SHA-256, address range, and target preflight result.
4. Tick the overwrite confirmation and select **Flash CUL868 V3**.
5. Wait for a final `V` or `VTS` response before using the radio again.

An upload is private to the app for at most 15 minutes, replaces a prior
unclaimed validation, and is deleted after a flash attempt. Only one operation
can be queued or running. Once shutdown is requested, later upload and flash
handoffs are rejected before queued work is discarded. A request accepted
immediately before that boundary is reported as discarded and is never flashed
after shutdown begins.

## Recovery Safety

The normal application and its DFU bootloader may expose different USB serial
descriptors. The app saves the physical USB topology, the configured normal
serial path, and a non-secret descriptor binding after a verified `V`/`VTS`
response.

Immediately before it sends `B01`, the app writes a short-lived
`handoff-pending` recovery record. A changed descriptor serial is accepted only
in that post-`B01` window, which is no longer than the configured post-DFU
timeout. Once it sees the bootloader, the record changes to
`observed-bootloader` and every destructive DFU command is bound to that
bootloader's current topology and descriptor serial.

If a later recovery sees a different bootloader serial, the app does not flash
it automatically. Validation offers one-time manual recovery only when exactly
one expected bootloader is visible, and requires a second confirmation that it
is the physical configured CUL868. This also applies to pre-phase state written
by earlier app versions when its descriptor serial changed. A matching serial
on the saved topology remains eligible for automatic recovery.

If the app has never verified the radio and it is already in DFU mode, the same
one-time recovery path is available only for exactly one `03eb:2ff4` device.
More than one candidate, no candidate, or a changed configured device path is
refused. The browser never chooses a target; the server retains and rechecks the
selected topology and serial immediately before each DFU command.

After `B01`, the previously displayed firmware version is deliberately marked
unknown. If DFU or final verification fails, paused CUL consumers stay stopped
to prevent them from reopening a radio with unknown firmware.

## CUL Consumers

The app detects and coordinates matching, currently running:

- [wmbusmeters](https://github.com/wmbusmeters/wmbusmeters-ha-addon) through
  its documented `device` setting, including `auto` and `cul` discovery modes.
- [MAX! to MQTT Bridge (`max2mqtt`)](https://github.com/pwurbs/max2mqtt)
  through its exact `serial_port` option.

After a verified firmware change, CULFW-family descriptor changes can rename a
`/dev/serial/by-id/...` alias. The app updates only one direct, unambiguous
replacement alias on its own option and known paused consumers. It never rewrites
`auto`, `cul`, a raw `/dev/ttyACM*` path, an unrelated path, or private
configuration files.

FHEM, Homegear, Node-RED, MaxCUL, remote services, and custom Core integrations
can also own a CUL. Add the exact slug of a Home Assistant app to **Additional
CUL consumer apps** to pause it, or stop unmanaged consumers manually. An
explicitly listed app remains stopped after a descriptor alias migration so its
private configuration can be reviewed safely.

## Virtual Machines

The radio changes identity between `03eb:204b` and `03eb:2ff4`. Pass both
personalities through to Home Assistant. A physical USB port or controller
mapping is generally more reliable than VID:PID-only assignment. Enable the
QEMU workaround only if QEMU actually changes the guest USB topology; it adds
an 8-second settle delay and permits an exact-one fallback only during the
verified `B01` to final `V`/`VTS` transition. It cannot repair a passthrough
mapping that removes the device from the guest.

## Security And Licensing

- The UI is Home Assistant Ingress only. Mutating requests require the trusted
  Ingress proxy and the Ingress request header.
- The worker invokes `dfu-programmer` without a shell, using an exact USB
  bus/address selector. It does not pass Supervisor credentials to the child.
- The AppArmor child profile gives `dfu-programmer` raw USB access but no
  network permission. The Python process has no raw USB device-node access.
- The app requests Home Assistant's `manager` role solely for precise consumer
  option/lifecycle coordination. Install it only from a trusted repository.

The app's Python and web code is Apache-2.0. It builds and invokes the
unmodified GPL-2.0 `dfu-programmer` v1.1.0 as a separate executable and ships
its complete corresponding source in the image. Alpine's dynamically linked
`libusb` package is LGPL-2.1-or-later; matching source and license information
are also included. See [LICENSES/COMPONENTS.md](LICENSES/COMPONENTS.md) for the
runtime component inventory. CULFW, a-culfw, TSCULFW, LUFA, and wmbusmeters are
not included; their licensing applies to firmware obtained independently. This
is not legal advice.
