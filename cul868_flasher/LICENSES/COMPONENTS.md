# Runtime components

This add-on's Python application code is Apache-2.0. The complete Apache-2.0
text is installed as `LICENSE` in the image and is present here as
`Apache-2.0.txt`.

| Component | Version / source | License | How it is used |
| --- | --- | --- | --- |
| dfu-programmer | v1.1.0, `f23d6dfd6671b46cbf1279a61291f6fa18d7559e` | GPL-2.0 | Separate `dfu-programmer` executable for the Atmel-compatible DFU protocol. Its unmodified complete source and `COPYING` file are installed at `/usr/src/dfu-programmer`. |
| libusb | Alpine `libusb` 1.0.30-r0, from upstream libusb 1.0.30 | LGPL-2.1-or-later | Dynamically linked by dfu-programmer. Its license text is installed at `/usr/share/licenses/cul868-flasher/LGPL-2.1-or-later.txt`; complete checksum-verified corresponding source is installed at `/usr/src/libusb-1.0.30`. |
| Python | Home Assistant Alpine base image | PSF-2.0 | Standard-library-only application runtime. |

The Docker build stage also uses Alpine's compiler, Autoconf, Automake,
Libtool, Git, and `libusb-dev` packages. They are build-only tools and are not
copied into the final image. The final image writes its installed Alpine package
name/version inventory to `/usr/share/licenses/cul868-flasher/alpine-packages.tsv`.

The image uses the system shared-library mechanism for `libusb`; replacing it
with a compatible library remains possible without relinking the separately
distributed `dfu-programmer` executable. The source archive is downloaded from
the upstream v1.0.30 release and verified against the SHA-512 recorded in
Alpine 3.24's `libusb` package recipe. This add-on does not modify `libusb`.

No CULFW, a-culfw, TSCUL, LUFA, Nordic, or wmbusmeters source or firmware is
copied into this add-on. The user supplies the firmware HEX file. Those
projects' licenses remain relevant to the firmware the user chooses to flash.

`dfu-programmer` is invoked as an independent process; the add-on does not
link against or modify it. This is a component inventory, not legal advice.
