from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_addon_requests_only_needed_hardware_and_manager_access(self) -> None:
        config = (ROOT / "cul868_flasher" / "config.yaml").read_text(encoding="utf-8")
        self.assertIn("hassio_role: manager", config)
        self.assertIn("apparmor: true", config)
        self.assertIn("uart: true", config)
        self.assertIn("usb: true", config)
        self.assertIn("tmpfs: true", config)
        self.assertNotIn("full_access: true", config)

    def test_dfu_child_profile_has_raw_usb_but_no_network(self) -> None:
        profile = (ROOT / "cul868_flasher" / "apparmor.txt").read_text(encoding="utf-8")
        child = profile.split("profile cul868_flasher_dfu_programmer", maxsplit=1)[1]
        self.assertIn("/dev/bus/usb/*/* rw,", child)
        self.assertNotIn("network", child)
        self.assertIn("/usr/local/bin/dfu-programmer cx -> cul868_flasher_dfu_programmer", profile)

    def test_dockerfile_ships_dfu_programmer_source_with_the_binary(self) -> None:
        dockerfile = (ROOT / "cul868_flasher" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("DFU_PROGRAMMER_VERSION=v1.1.0", dockerfile)
        self.assertIn("LIBUSB_APK_VERSION=1.0.30-r0", dockerfile)
        self.assertIn("LIBUSB_SHA512=", dockerfile)
        self.assertIn("COPY --from=dfu-programmer-builder /src/dfu-programmer /usr/src/dfu-programmer", dockerfile)
        self.assertIn("COPY --from=dfu-programmer-builder /src/dfu-programmer/src/dfu-programmer", dockerfile)
        self.assertIn("COPY --from=libusb-source /src/libusb /usr/src/libusb-1.0.30", dockerfile)
        self.assertIn("COPY LICENSES/LGPL-2.1-or-later.txt", dockerfile)
        self.assertIn('"{name}\\t{version}\\n"', dockerfile)

    def test_docker_context_excludes_generated_python_bytecode(self) -> None:
        ignore = (ROOT / "cul868_flasher" / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("__pycache__/", ignore)
        self.assertIn("*.py[cod]", ignore)
