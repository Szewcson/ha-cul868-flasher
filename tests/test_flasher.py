from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from app.flasher import Cul868Flasher, FlashError
from app.hexfile import parse_hex_file
from app.models import Settings
from app.state import DeviceStateStore, KnownDevice
from app.supervisor import WmbusmetersPause

from .helpers import FakeSerialFactory, FakeSupervisor, FakeTopology, minimal_hex, target


class FlasherTests(unittest.TestCase):
    def _staged_image(self) -> tuple[Path, object]:
        descriptor, name = tempfile.mkstemp(prefix="cul868-", suffix=".hex", dir="/tmp")
        path = Path(name)
        with open(descriptor, "wb", closefd=True) as output:
            output.write(minimal_hex())
        return path, parse_hex_file(path)

    @staticmethod
    def _settings(qemu: bool = False, device: Path | None = None) -> Settings:
        return Settings(
            device=device or Path("/dev/ttyACM0"),
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
                self.assertEqual(flasher.preflight().mode, "recovery")
                result = flasher.flash(image, lambda _percent, _message: None)

            self.assertEqual(result["previous_version"], "V 1.0 CUL868")
            self.assertEqual(len(serial_factory.sessions), 1)
        finally:
            path.unlink(missing_ok=True)

    def test_status_exposes_a_known_bootloader_for_recovery(self) -> None:
        topology = FakeTopology(mode="bootloader", serial="CUL-TEST")
        with tempfile.TemporaryDirectory() as state_directory:
            state = DeviceStateStore(Path(state_directory))
            state.save(KnownDevice("2-3", "CUL-TEST", None, "/dev/ttyACM0"))
            flasher = Cul868Flasher(
                self._settings(),
                topology=topology,
                state_store=state,
                supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            )

            status = flasher.status()

        self.assertEqual(status["state"], "bootloader")
        self.assertEqual(status["topology"], "2-3")
        self.assertEqual(status["usb_serial"], "CUL-TEST")
        self.assertIsNone(status["last_verified_version"])

    def test_unpaired_bootloader_needs_explicit_one_time_recovery(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology(mode="bootloader", serial="CUL-TEST")
            supervisor = FakeSupervisor()
            serial_factory = FakeSerialFactory(topology, ["V 3.0 CUL868"])

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                if command[-1] == "start":
                    topology.mode = "application"
                return subprocess.CompletedProcess(command, 0)

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
                with self.assertRaisesRegex(FlashError, "requires explicit confirmation"):
                    flasher.flash(image, lambda _percent, _message: None)
                self.assertEqual(supervisor.events, [])
                preflight = flasher.preflight()
                status = flasher.status()
                self.assertEqual(preflight.mode, "manual-recovery")
                self.assertIsNotNone(preflight.manual_recovery)
                assert preflight.manual_recovery is not None
                result = flasher.flash(
                    image,
                    lambda _percent, _message: None,
                    manual_recovery=preflight.manual_recovery,
                )
                known = state.load()

            self.assertEqual(status["state"], "unpaired_bootloader")
            self.assertIsNone(result["previous_version"])
            self.assertEqual(result["installed_version"], "V 3.0 CUL868")
            self.assertEqual(supervisor.events, ["stop", "start"])
            self.assertIsNotNone(known)
            assert known is not None
            self.assertEqual(known.topology, "2-3")
            self.assertEqual(known.usb_serial, "CUL-TEST")
            self.assertEqual(known.version, "V 3.0 CUL868")
        finally:
            path.unlink(missing_ok=True)

    def test_unpaired_recovery_rechecks_the_selected_bootloader(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology(mode="bootloader", serial="CUL-TEST")
            supervisor = FakeSupervisor()
            with tempfile.TemporaryDirectory() as state_directory:
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=DeviceStateStore(Path(state_directory)),
                    supervisor=supervisor,  # type: ignore[arg-type]
                )
                preflight = flasher.preflight()
                assert preflight.manual_recovery is not None
                topology.bootloader = target(bootloader=True, serial="OTHER-CUL")

                with self.assertRaisesRegex(FlashError, "USB serial number.*changed"):
                    flasher.flash(
                        image,
                        lambda _percent, _message: None,
                        manual_recovery=preflight.manual_recovery,
                    )

            self.assertEqual(supervisor.events, [])
        finally:
            path.unlink(missing_ok=True)

    def test_unpaired_recovery_rejects_ambiguous_bootloaders(self) -> None:
        class AmbiguousBootloaders(FakeTopology):
            def bootloader_targets(self) -> tuple:
                return (
                    self.bootloader,
                    target(topology="2-4", bootloader=True, serial="OTHER-CUL"),
                )

        topology = AmbiguousBootloaders(mode="bootloader")
        with tempfile.TemporaryDirectory() as state_directory:
            flasher = Cul868Flasher(
                self._settings(),
                topology=topology,
                state_store=DeviceStateStore(Path(state_directory)),
                supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            )

            with self.assertRaisesRegex(FlashError, "found 2 CUL868 DFU bootloaders"):
                flasher.preflight()

            status = flasher.status()
        self.assertEqual(status["state"], "unavailable")
        self.assertIn("found 2 CUL868 DFU bootloaders", status["message"])

    def test_failed_bootloader_transition_retains_only_unknown_recovery_state(self) -> None:
        class MissingBootloaderTopology(FakeTopology):
            def bootloader_for_topology(self, _topology: str) -> object:
                return None

        path, image = self._staged_image()
        try:
            topology = MissingBootloaderTopology()
            clock = [0.0]

            def sleep_for(seconds: float) -> None:
                clock[0] += seconds

            with tempfile.TemporaryDirectory() as state_directory:
                state = DeviceStateStore(Path(state_directory))
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=state,
                    supervisor=FakeSupervisor(),  # type: ignore[arg-type]
                    serial_factory=FakeSerialFactory(topology, ["V 1.0 CUL868"]),  # type: ignore[arg-type]
                    sleep_fn=sleep_for,
                    monotonic_fn=lambda: clock[0],
                )
                with self.assertRaisesRegex(FlashError, "DFU bootloader 03eb:2ff4 did not appear"):
                    flasher.flash(image, lambda _percent, _message: None)
                known = state.load()

            self.assertIsNotNone(known)
            assert known is not None
            self.assertEqual(known.topology, "2-3")
            self.assertEqual(known.usb_serial, "CUL-TEST")
            self.assertEqual(known.configured_device, "/dev/ttyACM0")
            self.assertIsNone(known.version)
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

    def test_interrupted_handoff_recovers_when_bootloader_uses_its_own_serial(self) -> None:
        """A B01 handoff saved before DFU can recover a standard Atmel descriptor."""

        topology = FakeTopology(mode="bootloader", serial="AT32U4-DFU")
        with tempfile.TemporaryDirectory() as state_directory:
            state = DeviceStateStore(Path(state_directory))
            state.save(KnownDevice("2-3", "TSCULFW-CUL868", None, "/dev/ttyACM0"))
            flasher = Cul868Flasher(
                self._settings(),
                topology=topology,
                state_store=state,
                supervisor=FakeSupervisor(),  # type: ignore[arg-type]
            )

            preflight = flasher.preflight()

        self.assertEqual(preflight.mode, "recovery")
        self.assertEqual(preflight.topology, "2-3")

    def test_changed_configured_path_requires_explicit_unpaired_recovery(self) -> None:
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

            with self.assertRaisesRegex(FlashError, "path changed.*automatic bootloader recovery"):
                flasher._plan()
            preflight = flasher.preflight()

        self.assertEqual(preflight.mode, "manual-recovery")
        self.assertIsNotNone(preflight.manual_recovery)

    def test_startup_verification_records_version_and_restores_wmbusmeters(self) -> None:
        topology = FakeTopology()
        supervisor = FakeSupervisor()
        serial_factory = FakeSerialFactory(topology, ["V 1.2 CUL868"])
        with tempfile.TemporaryDirectory() as state_directory:
            state = DeviceStateStore(Path(state_directory))
            flasher = Cul868Flasher(
                self._settings(),
                topology=topology,
                state_store=state,
                supervisor=supervisor,  # type: ignore[arg-type]
                serial_factory=serial_factory,  # type: ignore[arg-type]
            )
            version = flasher.verify_running_application()
            known = state.load()

        self.assertEqual(version, "V 1.2 CUL868")
        self.assertEqual(supervisor.events, ["stop", "start"])
        self.assertEqual(len(serial_factory.sessions), 1)
        self.assertFalse(serial_factory.sessions[0].entered_bootloader)
        self.assertIsNotNone(known)
        assert known is not None
        self.assertEqual(known.version, "V 1.2 CUL868")
        self.assertEqual(known.configured_device, "/dev/ttyACM0")

    def test_startup_verification_does_not_pause_wmbusmeters_in_dfu_mode(self) -> None:
        topology = FakeTopology(mode="bootloader")
        supervisor = FakeSupervisor()
        flasher = Cul868Flasher(
            self._settings(),
            topology=topology,
            supervisor=supervisor,  # type: ignore[arg-type]
        )

        with self.assertRaisesRegex(FlashError, "configured CUL application is unavailable"):
            flasher.verify_running_application()

        self.assertEqual(supervisor.events, [])

    def test_failure_after_reset_keeps_recovery_state_and_leaves_wmbusmeters_stopped(self) -> None:
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
            self.assertEqual(supervisor.events, ["stop"])
        finally:
            path.unlink(missing_ok=True)

    def test_failed_manual_recovery_leaves_wmbusmeters_stopped(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology(mode="bootloader")
            supervisor = FakeSupervisor()

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                return subprocess.CompletedProcess(command, 1)

            with tempfile.TemporaryDirectory() as state_directory:
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=DeviceStateStore(Path(state_directory)),
                    supervisor=supervisor,  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                )
                preflight = flasher.preflight()
                assert preflight.manual_recovery is not None

                with self.assertRaisesRegex(FlashError, "dfu-programmer erase failed"):
                    flasher.flash(
                        image,
                        lambda _percent, _message: None,
                        manual_recovery=preflight.manual_recovery,
                    )

            self.assertEqual(supervisor.events, ["stop"])
        finally:
            path.unlink(missing_ok=True)

    def test_failure_before_dfu_restores_wmbusmeters(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            supervisor = FakeSupervisor()
            with tempfile.TemporaryDirectory() as state_directory:
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=DeviceStateStore(Path(state_directory)),
                    supervisor=supervisor,  # type: ignore[arg-type]
                    serial_factory=FakeSerialFactory(topology, []),  # type: ignore[arg-type]
                    sleep_fn=lambda _seconds: None,
                )

                with self.assertRaisesRegex(FlashError, "after 3 attempts"):
                    flasher.flash(image, lambda _percent, _message: None)

            self.assertEqual(supervisor.events, ["stop", "start"])
        finally:
            path.unlink(missing_ok=True)

    def test_start_transport_error_reconciles_a_running_application(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            serial_factory = FakeSerialFactory(topology, ["V old CUL868", "V recovered CUL868"])
            commands: list[list[str]] = []

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                commands.append(command)
                if command[-1] == "start":
                    topology.mode = "application"
                    return subprocess.CompletedProcess(command, 1)
                return subprocess.CompletedProcess(command, 0)

            with tempfile.TemporaryDirectory() as state_directory:
                state = DeviceStateStore(Path(state_directory))
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=state,
                    supervisor=FakeSupervisor(),  # type: ignore[arg-type]
                    serial_factory=serial_factory,  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                )
                result = flasher.flash(image, lambda _percent, _message: None)
                known = state.load()

            self.assertEqual(result["installed_version"], "V recovered CUL868")
            self.assertEqual([command[2] for command in commands], ["erase", "flash", "start"])
            self.assertIsNotNone(known)
            assert known is not None
            self.assertEqual(known.version, "V recovered CUL868")
        finally:
            path.unlink(missing_ok=True)

    def test_post_dfu_verification_waits_for_a_culfw_restart(self) -> None:
        """A CUL endpoint alone is insufficient evidence after a firmware write."""

        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            serial_factory = FakeSerialFactory(
                topology,
                [
                    "V 1.67 CUL868",
                    RuntimeError("CUL is still restarting"),
                    RuntimeError("CUL is still restarting"),
                    "V 1.26.08 a-culfw Build: test CUL868 (F-Band: 868MHz)",
                ],
            )
            clock = [0.0]
            reports: list[str] = []

            def sleep_for(seconds: float) -> None:
                clock[0] += seconds

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                if command[-1] == "start":
                    topology.mode = "application"
                return subprocess.CompletedProcess(command, 0)

            with tempfile.TemporaryDirectory() as state_directory:
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=DeviceStateStore(Path(state_directory)),
                    supervisor=FakeSupervisor(),  # type: ignore[arg-type]
                    serial_factory=serial_factory,  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                    sleep_fn=sleep_for,
                    monotonic_fn=lambda: clock[0],
                )
                result = flasher.flash(image, lambda _percent, message: reports.append(message))

            self.assertEqual(
                result["installed_version"],
                "V 1.26.08 a-culfw Build: test CUL868 (F-Band: 868MHz)",
            )
            self.assertEqual(clock[0], 2)
            self.assertIn(
                "Waiting for the CUL868 application serial interface to become ready.", reports
            )
        finally:
            path.unlink(missing_ok=True)

    def test_post_dfu_verification_waits_for_tsculf_usb_reconnect(self) -> None:
        """TSCULFW must reconnect its CDC endpoint before the V request is sent."""

        path, image = self._staged_image()
        try:
            clock = [0.0]

            class DelayedTsculfTopology(FakeTopology):
                def application_for_topology(self, topology: str) -> object:
                    if clock[0] >= 5.5:
                        self.mode = "application"
                    return super().application_for_topology(topology)

            topology = DelayedTsculfTopology()
            serial_factory = FakeSerialFactory(
                topology,
                ["V 1.67 CUL868", "VTS 0.43 CUL868"],
            )

            def sleep_for(seconds: float) -> None:
                clock[0] += seconds

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                # TSCULFW keeps USB disconnected for about 5.5 seconds after
                # this command. The topology exposes its fresh CDC endpoint later.
                return subprocess.CompletedProcess(command, 0)

            with tempfile.TemporaryDirectory() as state_directory:
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=DeviceStateStore(Path(state_directory)),
                    supervisor=FakeSupervisor(),  # type: ignore[arg-type]
                    serial_factory=serial_factory,  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                    sleep_fn=sleep_for,
                    monotonic_fn=lambda: clock[0],
                )
                result = flasher.flash(image, lambda _percent, _message: None)

            self.assertEqual(result["installed_version"], "VTS 0.43 CUL868")
            self.assertGreaterEqual(clock[0], 5.5)
            self.assertEqual(len(serial_factory.sessions), 2)
        finally:
            path.unlink(missing_ok=True)

    def test_post_dfu_verification_failure_leaves_wmbusmeters_stopped(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            supervisor = FakeSupervisor()
            serial_factory = FakeSerialFactory(
                topology,
                ["V 1.67 CUL868"] + [RuntimeError("CUL is still restarting")] * 90,
            )
            clock = [0.0]

            def sleep_for(seconds: float) -> None:
                clock[0] += seconds

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                if command[-1] == "start":
                    topology.mode = "application"
                return subprocess.CompletedProcess(command, 0)

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
                    sleep_fn=sleep_for,
                    monotonic_fn=lambda: clock[0],
                )
                with self.assertRaisesRegex(FlashError, "90-second post-DFU timeout"):
                    flasher.flash(image, lambda _percent, _message: None)
                known = state.load()

            self.assertEqual(supervisor.events, ["stop"])
            self.assertEqual(clock[0], 90)
            self.assertIsNotNone(known)
            assert known is not None
            self.assertIsNone(known.version)
        finally:
            path.unlink(missing_ok=True)

    def test_post_dfu_verification_accepts_direct_firmware_descriptor_change(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            supervisor = FakeSupervisor()
            serial_factory = FakeSerialFactory(topology, ["V 1.67 CUL868", "VTS 0.43 CUL868"])

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                if command[-1] == "start":
                    topology.mode = "application"
                    topology.application = target(serial="OTHER-CUL")
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

            self.assertEqual(result["installed_version"], "VTS 0.43 CUL868")
            self.assertEqual(supervisor.events, ["stop", "start"])
        finally:
            path.unlink(missing_ok=True)

    def test_active_flash_accepts_a_different_bootloader_descriptor_serial(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            topology.bootloader = target(bootloader=True, serial="OTHER-CUL")
            supervisor = FakeSupervisor()
            serial_factory = FakeSerialFactory(
                topology, ["VTS 0.43 CUL868", "V 1.67 CUL868"]
            )

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
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

            self.assertEqual(result["installed_version"], "V 1.67 CUL868")
            self.assertEqual(supervisor.events, ["stop", "start"])
        finally:
            path.unlink(missing_ok=True)

    def test_flash_migrates_by_id_path_across_culfw_families(self) -> None:
        cases = (
            (
                Path("/dev/serial/by-id/usb-busware.de_CUL868-culfw-if00"),
                Path("/dev/serial/by-id/usb-busware.de_CUL868-tsculfw-if00"),
                "V 1.67 CUL868",
                "VTS 0.43 CUL868",
                "TSCULFW-CUL868",
            ),
            (
                Path("/dev/serial/by-id/usb-busware.de_CUL868-tsculfw-if00"),
                Path("/dev/serial/by-id/usb-busware.de_CUL868-a-culfw-if00"),
                "VTS 0.43 CUL868",
                "V 1.26.08 a-culfw Build: test CUL868 (F-Band: 868MHz)",
                "A-CULFW-CUL868",
            ),
        )
        for previous, current, before, after, usb_serial in cases:
            with self.subTest(before=before, after=after):
                path, image = self._staged_image()
                try:
                    class DescriptorChangingTopology(FakeTopology):
                        def by_id_paths_for_tty(
                            self, device: Path, expected_current: Path = current
                        ) -> tuple[Path, ...]:
                            if device != Path("/dev/ttyACM0"):
                                raise AssertionError(f"unexpected CUL endpoint: {device}")
                            return (expected_current,)

                    class RetargetingSupervisor(FakeSupervisor):
                        def __init__(self) -> None:
                            super().__init__()
                            self.wmbus_retargets: list[tuple[Path, Path]] = []
                            self.own_retargets: list[tuple[Path, Path]] = []

                        def retarget_paused_wmbusmeters(
                            self, _pause: object, old: Path, new: Path
                        ) -> tuple[str, ...]:
                            self.wmbus_retargets.append((old, new))
                            return ("wmbusmeters",)

                        def retarget_own_device_path(self, old: Path, new: Path) -> bool:
                            self.own_retargets.append((old, new))
                            return True

                    topology = DescriptorChangingTopology()
                    supervisor = RetargetingSupervisor()
                    serial_factory = FakeSerialFactory(topology, [before, after])

                    def runner(
                        command: list[str],
                        expected_topology: FakeTopology = topology,
                        expected_usb_serial: str = usb_serial,
                        **_kwargs: object,
                    ) -> subprocess.CompletedProcess[object]:
                        if command[-1] == "start":
                            expected_topology.mode = "application"
                            expected_topology.application = target(serial=expected_usb_serial)
                        return subprocess.CompletedProcess(command, 0)

                    with tempfile.TemporaryDirectory() as state_directory:
                        state = DeviceStateStore(Path(state_directory))
                        flasher = Cul868Flasher(
                            self._settings(device=previous),
                            topology=topology,
                            state_store=state,
                            supervisor=supervisor,  # type: ignore[arg-type]
                            serial_factory=serial_factory,  # type: ignore[arg-type]
                            dfu_executable="/bin/true",
                            runner=runner,
                        )
                        result = flasher.flash(image, lambda _percent, _message: None)
                        known = state.load()

                    self.assertEqual(result["device"], str(current))
                    self.assertEqual(result["retargeted_wmbusmeters_addons"], ["wmbusmeters"])
                    self.assertEqual(supervisor.wmbus_retargets, [(previous, current)])
                    self.assertEqual(supervisor.own_retargets, [(previous, current)])
                    self.assertEqual(supervisor.events, ["stop", "start"])
                    self.assertIsNotNone(known)
                    assert known is not None
                    self.assertEqual(known.usb_serial, usb_serial)
                    self.assertEqual(known.configured_device, str(current))
                finally:
                    path.unlink(missing_ok=True)

    def test_flash_refuses_ambiguous_changed_by_id_paths_and_leaves_readers_stopped(self) -> None:
        path, image = self._staged_image()
        previous = Path("/dev/serial/by-id/usb-busware.de_CUL868-old-if00")
        try:
            class AmbiguousAliasTopology(FakeTopology):
                def by_id_paths_for_tty(self, _device: Path) -> tuple[Path, ...]:
                    return (
                        Path("/dev/serial/by-id/usb-busware.de_CUL868-first-if00"),
                        Path("/dev/serial/by-id/usb-busware.de_CUL868-second-if00"),
                    )

            topology = AmbiguousAliasTopology()
            supervisor = FakeSupervisor()
            serial_factory = FakeSerialFactory(topology, ["V 1.67 CUL868", "VTS 0.43 CUL868"])

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                if command[-1] == "start":
                    topology.mode = "application"
                    topology.application = target(serial="TSCULFW-CUL868")
                return subprocess.CompletedProcess(command, 0)

            with tempfile.TemporaryDirectory() as state_directory:
                state = DeviceStateStore(Path(state_directory))
                flasher = Cul868Flasher(
                    self._settings(device=previous),
                    topology=topology,
                    state_store=state,
                    supervisor=supervisor,  # type: ignore[arg-type]
                    serial_factory=serial_factory,  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                )
                with self.assertRaisesRegex(FlashError, "multiple /dev/serial/by-id aliases"):
                    flasher.flash(image, lambda _percent, _message: None)
                known = state.load()

            self.assertEqual(supervisor.events, ["stop"])
            self.assertIsNotNone(known)
            assert known is not None
            self.assertEqual(known.version, "VTS 0.43 CUL868")
            self.assertEqual(known.configured_device, str(previous))
        finally:
            path.unlink(missing_ok=True)

    def test_serial_by_id_migration_failure_keeps_wmbusmeters_paused(self) -> None:
        previous = Path("/dev/serial/by-id/usb-busware.de_CUL868-old-if00")
        current = Path("/dev/serial/by-id/usb-busware.de_CUL868-new-if00")

        class OneAliasTopology(FakeTopology):
            def by_id_paths_for_tty(self, _device: Path) -> tuple[Path, ...]:
                return (current,)

        class FailingSupervisor(FakeSupervisor):
            def retarget_paused_wmbusmeters(
                self, _pause: object, _old: Path, _new: Path
            ) -> tuple[str, ...]:
                raise RuntimeError("simulated Supervisor transport failure")

        supervisor = FailingSupervisor()
        flasher = Cul868Flasher(
            self._settings(device=previous),
            topology=OneAliasTopology(),
            supervisor=supervisor,  # type: ignore[arg-type]
        )
        pause = WmbusmetersPause(("wmbusmeters",))

        with self.assertRaisesRegex(FlashError, "serial-by-id path could not be migrated"):
            flasher._migrate_serial_by_id_path(
                previous,
                Path("/dev/ttyACM0"),
                pause,
                lambda _percent, _message: None,
            )

        self.assertFalse(pause.restore_after_error)

    def test_qemu_workaround_waits_after_each_identity_transition(self) -> None:
        path, image = self._staged_image()
        try:
            events: list[str] = []

            class RecordingTopology(FakeTopology):
                def bootloader_for_topology(self, topology: str) -> object:
                    events.append("bootloader probe")
                    return super().bootloader_for_topology(topology)

                def application_for_topology(self, topology: str) -> object:
                    events.append("application probe")
                    return super().application_for_topology(topology)

            topology = RecordingTopology()
            delays: list[float] = []
            serial_factory = FakeSerialFactory(topology, ["V old CUL868", "V new CUL868"])

            def sleep_for(seconds: float) -> None:
                delays.append(seconds)
                events.append(f"sleep {seconds}")

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
                    sleep_fn=sleep_for,
                )
                flasher.flash(image, lambda _percent, _message: None)

            self.assertEqual(delays, [8, 8])
            self.assertLess(events.index("sleep 8"), events.index("bootloader probe"))
            first_application_probe = events.index("application probe")
            self.assertEqual(events[first_application_probe - 1], "sleep 8")
        finally:
            path.unlink(missing_ok=True)

    def test_qemu_workaround_uses_a_unique_reassigned_guest_usb_path(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            topology.bootloader = target(topology="2-4", bootloader=True, address=8)
            serial_factory = FakeSerialFactory(topology, ["V old CUL868", "V new CUL868"])
            commands: list[list[str]] = []
            reports: list[str] = []

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                commands.append(command)
                if command[-1] == "start":
                    topology.mode = "application"
                return subprocess.CompletedProcess(command, 0)

            with tempfile.TemporaryDirectory() as state_directory:
                state = DeviceStateStore(Path(state_directory))
                flasher = Cul868Flasher(
                    self._settings(qemu=True),
                    topology=topology,
                    state_store=state,
                    supervisor=FakeSupervisor(),  # type: ignore[arg-type]
                    serial_factory=serial_factory,  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                    sleep_fn=lambda _seconds: None,
                )
                result = flasher.flash(image, lambda _percent, message: reports.append(message))
                known = state.load()

            self.assertEqual(result["topology"], "2-3")
            self.assertEqual(result["installed_version"], "V new CUL868")
            self.assertEqual(
                commands,
                [
                    ["/bin/true", "atmega32u4:2,8", "erase"],
                    ["/bin/true", "atmega32u4:2,8", "flash", str(path)],
                    ["/bin/true", "atmega32u4:2,8", "start"],
                ],
            )
            self.assertIn(
                "QEMU reattached the CUL DFU bootloader on guest USB path 2-4.", reports
            )
            self.assertIn("QEMU reattached the CUL application on guest USB path 2-3.", reports)
            self.assertIsNotNone(known)
            assert known is not None
            self.assertEqual(known.topology, "2-3")
        finally:
            path.unlink(missing_ok=True)

    def test_qemu_guest_path_change_requires_the_opt_in_workaround(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            topology.bootloader = target(topology="2-4", bootloader=True)
            clock = [0.0]

            def sleep_for(seconds: float) -> None:
                clock[0] += seconds

            with tempfile.TemporaryDirectory() as state_directory:
                flasher = Cul868Flasher(
                    self._settings(),
                    topology=topology,
                    state_store=DeviceStateStore(Path(state_directory)),
                    supervisor=FakeSupervisor(),  # type: ignore[arg-type]
                    serial_factory=FakeSerialFactory(topology, ["V old CUL868"]),  # type: ignore[arg-type]
                    sleep_fn=sleep_for,
                    monotonic_fn=lambda: clock[0],
                )

                with self.assertRaisesRegex(
                    FlashError,
                    "visible on guest USB path 2-4.*enable QEMU USB re-enumeration workaround",
                ):
                    flasher.flash(image, lambda _percent, _message: None)
        finally:
            path.unlink(missing_ok=True)

    def test_qemu_workaround_refuses_multiple_reassigned_dfu_targets(self) -> None:
        class AmbiguousQemuTopology(FakeTopology):
            def bootloader_targets(self) -> tuple:
                return (
                    self.bootloader,
                    target(topology="2-5", bootloader=True, serial="OTHER-CUL"),
                )

        path, image = self._staged_image()
        try:
            topology = AmbiguousQemuTopology()
            topology.bootloader = target(topology="2-4", bootloader=True)
            with tempfile.TemporaryDirectory() as state_directory:
                flasher = Cul868Flasher(
                    self._settings(qemu=True),
                    topology=topology,
                    state_store=DeviceStateStore(Path(state_directory)),
                    supervisor=FakeSupervisor(),  # type: ignore[arg-type]
                    serial_factory=FakeSerialFactory(topology, ["V old CUL868"]),  # type: ignore[arg-type]
                    sleep_fn=lambda _seconds: None,
                )

                with self.assertRaisesRegex(FlashError, "found 2 CUL868 DFU bootloader targets"):
                    flasher.flash(image, lambda _percent, _message: None)
        finally:
            path.unlink(missing_ok=True)

    def test_qemu_workaround_accepts_a_reassigned_dfu_descriptor_change_after_b01(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            topology.bootloader = target(topology="2-4", bootloader=True, serial="OTHER-CUL")
            commands: list[list[str]] = []

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                commands.append(command)
                if command[-1] == "start":
                    topology.mode = "application"
                return subprocess.CompletedProcess(command, 0)

            with tempfile.TemporaryDirectory() as state_directory:
                flasher = Cul868Flasher(
                    self._settings(qemu=True),
                    topology=topology,
                    state_store=DeviceStateStore(Path(state_directory)),
                    supervisor=FakeSupervisor(),  # type: ignore[arg-type]
                    serial_factory=FakeSerialFactory(
                        topology, ["V old CUL868", "V recovered CUL868"]
                    ),  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                    sleep_fn=lambda _seconds: None,
                )

                result = flasher.flash(image, lambda _percent, _message: None)

            self.assertEqual(result["installed_version"], "V recovered CUL868")
            self.assertEqual([command[2] for command in commands], ["erase", "flash", "start"])
        finally:
            path.unlink(missing_ok=True)

    def test_qemu_workaround_preserves_the_reassigned_path_after_dfu_failure(self) -> None:
        path, image = self._staged_image()
        try:
            topology = FakeTopology()
            topology.bootloader = target(
                topology="2-4", bootloader=True, serial="DFU-CUL", address=8
            )
            supervisor = FakeSupervisor()

            def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
                return subprocess.CompletedProcess(command, 1)

            with tempfile.TemporaryDirectory() as state_directory:
                state = DeviceStateStore(Path(state_directory))
                flasher = Cul868Flasher(
                    self._settings(qemu=True),
                    topology=topology,
                    state_store=state,
                    supervisor=supervisor,  # type: ignore[arg-type]
                    serial_factory=FakeSerialFactory(topology, ["V old CUL868"]),  # type: ignore[arg-type]
                    dfu_executable="/bin/true",
                    runner=runner,
                    sleep_fn=lambda _seconds: None,
                )

                with self.assertRaisesRegex(FlashError, "dfu-programmer erase failed"):
                    flasher.flash(image, lambda _percent, _message: None)
                known = state.load()

            self.assertIsNotNone(known)
            assert known is not None
            self.assertEqual(known.topology, "2-4")
            self.assertEqual(known.usb_serial, "DFU-CUL")
            self.assertIsNone(known.version)
            self.assertEqual(supervisor.events, ["stop"])
        finally:
            path.unlink(missing_ok=True)
