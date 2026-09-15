from __future__ import annotations

import re
import unittest
from pathlib import Path

from app import __version__

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
        self.assertIn("device: str", config)
        self.assertNotIn("device: device(subsystem=tty)", config)

    def test_dfu_child_profile_has_raw_usb_but_no_network(self) -> None:
        profile = (ROOT / "cul868_flasher" / "apparmor.txt").read_text(encoding="utf-8")
        child = profile.split("profile /usr/local/bin/dfu-programmer", maxsplit=1)[1]
        self.assertIn("/dev/bus/usb/*/* rw,", child)
        self.assertNotIn("network", child)
        self.assertIn("/usr/local/bin/dfu-programmer cx,", profile)
        self.assertNotIn("cx -> cul868_flasher_dfu_programmer", profile)

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

    def test_base_images_are_digest_pinned(self) -> None:
        dockerfile = (ROOT / "cul868_flasher" / "Dockerfile").read_text(encoding="utf-8")
        build = (ROOT / "cul868_flasher" / "build.yaml").read_text(encoding="utf-8")

        self.assertIn("amd64-base:3.24@sha256:", dockerfile)
        self.assertIn("aarch64-base:3.24@sha256:", build)
        self.assertIn("amd64-base:3.24@sha256:", build)

    def test_docker_context_excludes_generated_python_bytecode(self) -> None:
        ignore = (ROOT / "cul868_flasher" / ".dockerignore").read_text(encoding="utf-8")
        self.assertIn("__pycache__/", ignore)
        self.assertIn("*.py[cod]", ignore)

    def test_ingress_hidden_elements_cannot_be_overridden_by_component_layout(self) -> None:
        styles = (ROOT / "cul868_flasher" / "app" / "web" / "styles.css").read_text(
            encoding="utf-8"
        )
        self.assertIn("[hidden] { display: none !important; }", styles)

    def test_release_metadata_and_supervisor_user_agent_share_one_version(self) -> None:
        config = (ROOT / "cul868_flasher" / "config.yaml").read_text(encoding="utf-8")
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        supervisor = (ROOT / "cul868_flasher" / "app" / "supervisor.py").read_text(
            encoding="utf-8"
        )

        config_version = re.search(r"^version: ([^\n]+)$", config, re.MULTILINE)
        project_version = re.search(r'^version = "([^"]+)"$', project, re.MULTILINE)

        self.assertIsNotNone(config_version)
        self.assertIsNotNone(project_version)
        assert config_version is not None
        assert project_version is not None
        self.assertEqual(config_version.group(1), __version__)
        self.assertEqual(project_version.group(1), __version__)
        self.assertIn('"User-Agent": f"cul868-flasher/{__version__}"', supervisor)

    def test_addon_local_docs_describe_the_current_recovery_and_upload_rules(self) -> None:
        addon_readme = (ROOT / "cul868_flasher" / "README.md").read_text(encoding="utf-8")
        docs = (ROOT / "cul868_flasher" / "DOCS.md").read_text(encoding="utf-8")
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        ui = (ROOT / "cul868_flasher" / "app" / "web" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("DOCS.md", addon_readme)
        self.assertIn("handoff-pending", docs)
        self.assertIn("observed-bootloader", docs)
        self.assertIn("manual recovery", docs)
        self.assertIn("handoff-pending", root_readme)
        self.assertIn("observed-bootloader", root_readme)
        self.assertIn("15 minutes", ui)
        self.assertIn("replaced by a newer validation", ui)
