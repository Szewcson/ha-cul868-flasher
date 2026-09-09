from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.usb import ManualRecoveryTarget, UsbTopology, UsbTopologyError, _read_attribute


class UsbTopologyTests(unittest.TestCase):
    def test_manual_recovery_target_rejects_non_string_serial(self) -> None:
        with self.assertRaisesRegex(ValueError, "USB serial is invalid"):
            ManualRecoveryTarget("2-3", 1)  # type: ignore[arg-type]

    def test_reads_sysfs_attribute_without_using_synthetic_stat_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            attribute = Path(directory) / "idVendor"
            attribute.write_text("03eb\n", encoding="ascii")
            # sysfs commonly reports a page-sized stat result for a tiny value.
            # The reader must use its bounded read rather than that metadata.
            with patch.object(Path, "stat", side_effect=AssertionError("stat must not be used")):
                self.assertEqual(_read_attribute(attribute), "03eb")

    def _make_tree(self, root: Path, product_id: str = "204b") -> tuple[UsbTopology, Path]:
        usb_root = root / "sys-bus-usb"
        device = usb_root / "2-3"
        interface = device / "2-3:1.0"
        interface.mkdir(parents=True)
        for name, value in {
            "idVendor": "03eb\n",
            "idProduct": f"{product_id}\n",
            "busnum": "2\n",
            "devnum": "7\n",
            "serial": "CUL-TEST\n",
            "product": "CUL868\n",
        }.items():
            (device / name).write_text(value, encoding="ascii")
        class_tty = root / "sys-class-tty" / "ttyACM0"
        class_tty.mkdir(parents=True)
        (class_tty / "device").symlink_to(interface)
        dev_directory = root / "dev"
        dev_directory.mkdir()
        (dev_directory / "ttyACM0").touch()
        return (
            UsbTopology(
                sys_class_tty=root / "sys-class-tty",
                sys_bus_usb=usb_root,
                dev_directory=dev_directory,
            ),
            dev_directory / "ttyACM0",
        )

    def test_resolves_configured_application_and_tty_by_physical_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            topology, device = self._make_tree(Path(directory))
            target = topology.configured_application(device)
            targets = topology.application_targets()
            self.assertEqual(target.topology, "2-3")
            self.assertEqual(target.vid_pid, "03eb:204b")
            self.assertEqual(topology.tty_for_topology("2-3"), device)
            self.assertEqual(targets, (target,))

    def test_refuses_non_cul_serial_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            topology, device = self._make_tree(Path(directory), "ea60")
            with self.assertRaisesRegex(UsbTopologyError, "not the expected CUL application"):
                topology.configured_application(device)

    def test_finds_only_expected_dfu_identity_on_same_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            topology, _device = self._make_tree(root, "2ff4")
            target = topology.bootloader_for_topology("2-3")
            self.assertIsNotNone(target)
            assert target is not None
            self.assertEqual(target.vid_pid, "03eb:2ff4")
            self.assertIsNone(topology.application_for_topology("2-3"))

    def test_lists_expected_bootloader_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            topology, _device = self._make_tree(Path(directory), "2ff4")

            targets = topology.bootloader_targets()

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].topology, "2-3")
        self.assertEqual(targets[0].vid_pid, "03eb:2ff4")
