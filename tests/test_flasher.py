from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from app.flasher import Cul868Flasher, FlashError
from app.hexfile import parse_hex_file
from app.models import Settings
from app.state import DeviceStateStore, KnownDevice

from .helpers import FakeSerialFactory, FakeSupervisor, FakeTopology, minimal_hex


class FlasherTests(unittest.TestCase):
    def _staged_image(self) -> tuple[Path, object]:
        descriptor, name = tempfile.mkstemp(prefix="cul868-", suffix=".hex", dir="/tmp")
        path = Path(name)
        with open(descriptor, "wb", closefd=True) as output:
            output.write(minimal_hex())
        return path, parse_hex_file(path)

    @staticmethod
    def _settings(qemu: bool = False) -> Settings:
        return Settings(
            device=Path("/dev/ttyACM0"),
            baudrate=9600,
            boot_timeout=15,
            qemu_usb_reenumeration_workaround=qemu,
        )

    def test_normal_flash_uses_exact_current_usb_bus_and_address(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            serial_factory = FakeSerialFactory(topology, ["V 1.0 CUL868", "V 1.1 CUL868"])
            supervisor = FakeSupervisor()
            commands: list[list[str]] = []

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                commands.append(command)
                if command[-1] == "start":
                    topology.mode = "application"
                return subprocess.CompletedProcess(command, 0)

            with tempfile.TemporaryDirectory() as state_directory:
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=DeviceStateStore(Path(state_directory)),
                    supervisor=supervisor,  # type: ignore[arg-type]
                    serial_factory=serial_factory,  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                )
                result = flasher.flash(image, lambda _percent, _message: None)

            self.assertEqual(result["installed_version"], "V 1.1 CUL868")
            self.assertEqual(supervisor.events, ["stop", "start"])
            self.assertEqual(
                commands,
                [
                    ["/bin/true", "atmega32u4:2,7", "erase"],
                    ["/bin/true", "atmega32u4:2,7", "flash", str(path)],
                    ["/bin/true", "atmega32u4:2,7", "start"],
                ],
            )
            self.assertTrue(serial_factory.sessions[0].entered_bootloader)
        finally:
            path.unlink(missing_ok=True)

    def test_recovery_requires_saved_topology_and_matching_serial(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology(mode="bootloader", serial="CUL-TEST")
            serial_factory = FakeSerialFactory(topology, ["V 2.0 CUL868"])

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                if command[-1] == "start":
                    topology.mode = "application"
                return subprocess.CompletedProcess(command, 0)

            with tempfile.TemporaryDirectory() as state_directory:
                state = DeviceStateStore(Path(state_directory))
                state.save(
                    KnownDevice("2-3", "CUL-TEST", "V 1.0 CUL868", "/dev/ttyACM0")
                )
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=state,
                    supervisor=FakeSupervisor(),  # type: ignore[arg-type]
                    serial_factory=serial_factory,  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                )
                self.assertEqual(flasher.preflight()["mode"], "recovery")
                result = flasher.flash(image, lambda _percent, _message: None)

            self.assertEqual(result["previous_version"], "V 1.0 CUL868")
            self.assertEqual(len(serial_factory.sessions), 1)
        finally:
            path.unlink(missing_ok=True)

    def test_recovery_rejects_different_usb_serial_on_saved_path(self) -> None:
        topology = FakeTopology(mode="bootloader", serial="OTHER-CUL")
        with tempfile.TemporaryDirectory() as state_directory:
            state = DeviceStateStore(Path(state_directory))
            state.save(KnownDevice("2-3", "CUL-TEST", "V 1.0 CUL868", "/dev/ttyACM0"))
            flasher = Cul868Flasher(
                self._settings(),
                topology=topology,
                state_store=state,
                supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(FlashError, "different USB serial"):
                flasher.preflight()

    def test_recovery_rejects_a_changed_configured_serial_path(self) -> None:
        topology = FakeTopology(mode="bootloader", serial="CUL-TEST")
        with tempfile.TemporaryDirectory() as state_directory:
            state = DeviceStateStore(Path(state_directory))
            state.save(KnownDevice("2-3", "CUL-TEST", "V 1.0 CUL868", "/dev/ttyACM1"))
            flasher = Cul868Flasher(
                self._settings(),
                topology=topology,
                state_store=state,
                supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            )
            with self.assertRaisesRegex(FlashError, "serial path changed"):
                flasher.preflight()

    def test_failure_after_reset_keeps_recovery_state_and_restores_wmbusmeters(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            supervisor = FakeSupervisor()
            serial_factory = FakeSerialFactory(topology, ["V 1.0 CUL868"])

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                return subprocess.CompletedProcess(command, 1)

            with tempfile.TemporaryDirectory() as state_directory:
                state = DeviceStateStore(Path(state_directory))
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=state,
                    supervisor=supervisor,  # type: ignore[arg-type]
                    serial_factory=serial_factory,  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                )
                with self.assertRaisesRegex(FlashError, "dfu-programmer erase failed"):
                    flasher.flash(image, lambda _percent, _message: None)
                known = state.load()

            self.assertIsNotNone(known)
            assert known is not None
            self.assertEqual(known.topology, "2-3")
            self.assertIsNone(known.version)
            self.assertEqual(supervisor.events, ["stop", "start"])
        finally:
            path.unlink(missing_ok=True)

    def test_qemu_workaround_waits_after_each_identity_transition(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            delays: list[float] = []
            serial_factory = FakeSerialFactory(topology, ["V old CUL868", "V new CUL868"])

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                if command[-1] == "start":
                    topology.mode = "application"
                return subprocess.CompletedProcess(command, 0)

            with tempfile.TemporaryDirectory() as state_directory:
                flasher = Cul868Flasher(
                    self._settings(qemu=True),
                    topology=topology,
                    state_store=DeviceStateStore(Path(state_directory)),
                    supervisor=FakeSupervisor(),  # type: ignore[arg-type]
                    serial_factory=serial_factory,  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                    sleep_fn=delays.append,
                )
                flasher.flash(image, lambda _percent, _message: None)

            self.assertEqual(delays, [8, 8])
        finally:
            path.unlink(missing_ok=True)
