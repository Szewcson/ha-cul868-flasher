# CUL868 Firmware Flasher

Experimental Home Assistant app for deliberately flashing a user-supplied Intel
HEX image to one CUL868 V3 radio. It does not discover, download, build, or
automatically install firmware.

Configure the normal CUL serial path, preferably a stable
`/dev/serial/by-id/...` alias, then use the Ingress page to validate and
explicitly confirm each flash. The same page offers guarded CULFW/a-culfw LED
on/off controls after a verified `V` response. Read [DOCS.md](DOCS.md) before
using DFU recovery or the QEMU USB re-enumeration workaround.
