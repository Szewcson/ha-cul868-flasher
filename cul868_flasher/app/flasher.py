"""Safe, serial CUL868 V3 DFU orchestration."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from time import monotonic, sleep
from typing import Protocol

from .hexfile import HexImage, parse_hex_file
from .models import Settings
from .serial import CulSerial
from .state import DeviceStateStore, KnownDevice
from .supervisor import SupervisorClient
from .usb import UsbTarget, UsbTopology, UsbTopologyError


_DFU_EXECUTABLE = "/usr/local/bin/dfu-programmer"
_DFU_TIMEOUT_SECONDS = 60
_DFU_OUTPUT_BYTES = 4 * 1024
_POLL_SECONDS = 0.25
_QEMU_SETTLE_SECONDS = 8


class FlashError(RuntimeError):
    """The CUL868 update could not be completed safely."""


class SerialSession(Protocol):
    """The deliberately small serial surface required during a normal flash."""

    def __enter__(self) -> SerialSession: ...

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...

    def version(self) -> str: ...

    def enter_bootloader(self) -> None: ...


SerialFactory = Callable[[Path, int], AbstractContextManager[SerialSession]]
ProgressReporter = Callable[[int, str], None]


@dataclass(frozen=True)
class _FlashPlan:
    mode: str
    topology: str
    application: UsbTarget | None
    bootloader: UsbTarget | None
    known: KnownDevice | None


class Cul868Flasher:
    """Flash exactly one CUL868 V3 on the configured or saved USB topology."""

    def __init__(
        self,
        settings: Settings,
        *,
        topology: UsbTopology | None = None,
        state_store: DeviceStateStore | None = None,
        supervisor: SupervisorClient | None = None,
        serial_factory: SerialFactory = CulSerial,
        dfu_executable: str = _DFU_EXECUTABLE,
        runner: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
        sleep_fn: Callable[[float], None] = sleep,
        monotonic_fn: Callable[[], float] = monotonic,
    ) -> None:
        self._settings = settings
        self._topology = topology or UsbTopology()
        self._state = state_store or DeviceStateStore(Path("/data"))
        self._supervisor = supervisor or SupervisorClient.from_environment()
        self._serial_factory = serial_factory
        self._dfu_executable = dfu_executable
        self._runner = runner
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._flash_lock = Lock()

    def status(self) -> dict[str, object]:
        """Report USB presence without opening the serial device or stopping apps."""

        known = self._state.load()
        known_for_config = known if self._state_matches_configuration(known) else None
        try:
            application = self._topology.configured_application(self._settings.device)
        except UsbTopologyError as application_error:
            if known_for_config is not None:
                try:
                    bootloader = self._bootloader_for_known(known_for_config)
                except FlashError as bootloader_error:
                    return self._status_error(known_for_config, str(bootloader_error))
                if bootloader is not None:
                    return {
                        "state": "bootloader",
                        "topology": known_for_config.topology,
                        "usb_serial": bootloader.usb_serial,
                        "last_verified_version": known_for_config.version,
                        "message": "CUL868 is in the known USB DFU bootloader and can be recovered.",
                    }
            return self._status_error(known_for_config, str(application_error))
        return {
            "state": "application",
            "topology": application.topology,
            "usb_serial": application.usb_serial,
            "last_verified_version": self._known_version_for_application(known_for_config, application),
            "message": "Configured CUL application USB endpoint is present.",
        }

    def preflight(self) -> dict[str, object]:
        """Resolve a safe target before accepting a confirmation to flash it."""

        plan = self._plan()
        if plan.mode == "application":
            return {
                "mode": "application",
                "topology": plan.topology,
                "message": "Configured CUL endpoint found. It will be verified with V before flashing.",
            }
        return {
            "mode": "recovery",
            "topology": plan.topology,
            "message": "Known CUL868 USB DFU bootloader found. Recovery will flash this same USB path.",
        }

    def verify_running_application(self) -> str:
        """Read and persist the normal CUL version without changing firmware.

        Startup verification takes the same exclusive serial ownership route as
        flashing: only matching wmbusmeters instances are paused, and their
        original lifecycle state is restored by the Supervisor context manager.
        A bootloader-only device deliberately does not qualify for this probe.
        """

        with self._flash_lock:
            try:
                application = self._topology.configured_application(self._settings.device)
            except UsbTopologyError as err:
                raise FlashError(f"configured CUL application is unavailable: {err}") from err
            try:
                with self._supervisor.temporarily_stop_wmbusmeters(self._settings.device):
                    version = self._read_version(self._settings.device)
                    self._state.save(
                        KnownDevice(
                            application.topology,
                            application.usb_serial,
                            version,
                            str(self._settings.device),
                        )
                    )
            except FlashError:
                raise
            except Exception as err:
                raise FlashError(f"could not verify the running CUL868 firmware: {err}") from err
            return version

    def flash(self, image: HexImage, report: ProgressReporter) -> dict[str, object]:
        """Execute one non-interruptible verified-image DFU transaction.

        A process-local lock is a final guard in addition to the HTTP operation
        queue. It prevents future callers from accidentally racing raw USB DFU.
        """

        with self._flash_lock:
            image = self._revalidate_image(image)
            report(3, "Resolving the configured CUL868 USB target.")
            plan = self._plan()
            with self._supervisor.temporarily_stop_wmbusmeters(self._settings.device) as paused_addons:
                expected_usb_serial = (
                    plan.application.usb_serial
                    if plan.application is not None
                    else plan.known.usb_serial if plan.known is not None else None
                )
                if plan.mode == "application":
                    before = self._enter_bootloader(plan, report)
                else:
                    before = plan.known.version if plan.known else None
                    report(15, "Using the saved CUL868 USB path in DFU recovery mode.")

                self._wait_for_bootloader(plan.topology, expected_usb_serial)
                self._settle_after_usb_change(report, "USB DFU bootloader")

                report(35, "Running the standard CUL DFU erase command.")
                # Once erase is attempted, the previous application version is
                # no longer trustworthy. Keep only the verified topology so a
                # later recovery remains safe without displaying stale state.
                self._state.save(
                    KnownDevice(plan.topology, expected_usb_serial, None, str(self._settings.device))
                )
                self._run_dfu(plan.topology, expected_usb_serial, "erase")
                report(55, "Writing the validated firmware image.")
                self._run_dfu(plan.topology, expected_usb_serial, "flash", str(image.path))
                report(78, "Starting the new CUL868 firmware.")
                self._run_dfu(plan.topology, expected_usb_serial, "start")
                self._settle_after_usb_change(report, "CUL868 application")

                application, device = self._wait_for_application(plan.topology)
                report(90, "Verifying the restarted CUL868 firmware.")
                after = self._read_version(device)
                self._state.save(
                    KnownDevice(
                        application.topology,
                        application.usb_serial,
                        after,
                        str(self._settings.device),
                    )
                )

            return {
                "previous_version": before,
                "installed_version": after,
                "topology": plan.topology,
                "paused_wmbusmeters_addons": list(paused_addons),
                "firmware_sha256": image.sha256,
                "firmware_bytes": image.data_bytes,
            }

    def _plan(self) -> _FlashPlan:
        known = self._state.load()
        known_for_config = known if self._state_matches_configuration(known) else None
        try:
            application = self._topology.configured_application(self._settings.device)
        except UsbTopologyError as application_error:
            if known is not None and known_for_config is None:
                raise FlashError(
                    "configured CUL serial path changed since the last verified device; "
                    "refusing bootloader recovery"
                ) from application_error
            if known_for_config is not None:
                try:
                    bootloader = self._bootloader_for_known(known_for_config)
                except FlashError as bootloader_error:
                    raise FlashError(str(bootloader_error)) from bootloader_error
                if bootloader is not None:
                    return _FlashPlan(
                        "recovery", known_for_config.topology, None, bootloader, known_for_config
                    )
            raise FlashError(
                f"configured CUL application is unavailable: {application_error}. "
                "Recovery is permitted only for a bootloader at a previously verified USB path."
            ) from application_error
        return _FlashPlan("application", application.topology, application, None, known)

    def _state_matches_configuration(self, known: KnownDevice | None) -> bool:
        """Require the exact configured path that originally verified recovery state."""

        return known is not None and known.configured_device == str(self._settings.device)

    @staticmethod
    def _known_version_for_application(
        known: KnownDevice | None, application: UsbTarget
    ) -> str | None:
        if known is None or known.topology != application.topology:
            return None
        if (
            known.usb_serial is not None
            and application.usb_serial is not None
            and known.usb_serial != application.usb_serial
        ):
            return None
        return known.version

    def _bootloader_for_known(self, known: KnownDevice) -> UsbTarget | None:
        try:
            bootloader = self._topology.bootloader_for_topology(known.topology)
        except UsbTopologyError as err:
            raise FlashError(str(err)) from err
        if (
            bootloader is not None
            and known.usb_serial is not None
            and bootloader.usb_serial is not None
            and known.usb_serial != bootloader.usb_serial
        ):
            raise FlashError(
                "DFU device on the saved USB path has a different USB serial number; refusing recovery"
            )
        return bootloader

    @staticmethod
    def _revalidate_image(image: HexImage) -> HexImage:
        """Defend the queue-to-worker handoff against a mutated staged file."""

        try:
            details = image.path.lstat()
        except OSError as err:
            raise FlashError("validated firmware image is no longer available") from err
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_mode & 0o077
            or image.path.parent != Path("/tmp")
            or not image.path.name.startswith("cul868-")
            or image.path.suffix != ".hex"
        ):
            raise FlashError("validated firmware image no longer has the required private staging form")
        try:
            checked = parse_hex_file(image.path)
        except Exception as err:
            raise FlashError(f"validated firmware image changed before flashing: {err}") from err
        if checked != image:
            raise FlashError("validated firmware image changed before flashing")
        return checked

    def _enter_bootloader(self, plan: _FlashPlan, report: ProgressReporter) -> str:
        if plan.application is None:
            raise FlashError("internal error: application flash plan has no USB target")
        report(10, "Verifying that the selected USB serial device is a CUL868.")
        try:
            with self._serial_factory(self._settings.device, self._settings.baudrate) as serial:
                before = serial.version()
                self._state.save(
                    KnownDevice(
                        plan.application.topology,
                        plan.application.usb_serial,
                        before,
                        str(self._settings.device),
                    )
                )
                report(20, "Requesting the verified CUL868 application to enter USB DFU mode.")
                serial.enter_bootloader()
        except Exception as err:
            raise FlashError(f"could not verify or reset the selected CUL868: {err}") from err
        return before

    def _wait_for_bootloader(self, topology: str, expected_usb_serial: str | None = None) -> UsbTarget:
        deadline = self._monotonic() + self._settings.boot_timeout
        last_error = "DFU bootloader has not appeared"
        while self._monotonic() < deadline:
            try:
                bootloader = self._topology.bootloader_for_topology(topology)
            except UsbTopologyError as err:
                last_error = str(err)
            else:
                if bootloader is not None:
                    if (
                        expected_usb_serial is not None
                        and bootloader.usb_serial is not None
                        and bootloader.usb_serial != expected_usb_serial
                    ):
                        raise FlashError(
                            "DFU device on the selected USB path has a different USB serial number"
                        )
                    return bootloader
            self._sleep(_POLL_SECONDS)
        raise FlashError(
            f"CUL868 DFU bootloader 03eb:2ff4 did not appear on USB path {topology}: {last_error}"
        )

    def _wait_for_application(self, topology: str) -> tuple[UsbTarget, Path]:
        deadline = self._monotonic() + self._settings.boot_timeout
        last_error = "CUL application has not appeared"
        while self._monotonic() < deadline:
            try:
                application = self._topology.application_for_topology(topology)
                device = self._topology.tty_for_topology(topology)
            except UsbTopologyError as err:
                last_error = str(err)
            else:
                if application is not None and device is not None:
                    return application, device
            self._sleep(_POLL_SECONDS)
        raise FlashError(
            f"CUL868 application 03eb:204b did not return on USB path {topology}: {last_error}"
        )

    def _read_version(self, device: Path) -> str:
        try:
            with self._serial_factory(device, self._settings.baudrate) as serial:
                return serial.version()
        except Exception as err:
            raise FlashError(f"CUL868 did not provide a valid V response after flashing: {err}") from err

    def _run_dfu(
        self, topology: str, expected_usb_serial: str | None, command: str, *arguments: str
    ) -> None:
        if command not in {"erase", "flash", "start"}:
            raise FlashError("internal error: unsupported DFU command")
        if shutil.which(self._dfu_executable) is None and not Path(self._dfu_executable).is_file():
            raise FlashError("dfu-programmer is not available in the add-on image")
        bootloader = self._wait_for_bootloader(topology, expected_usb_serial)
        selector = f"atmega32u4:{bootloader.bus_number},{bootloader.device_address}"
        command_line = [self._dfu_executable, selector, command, *arguments]
        environment = {
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        }
        try:
            with tempfile.TemporaryFile(mode="w+b", dir="/tmp") as output:
                completed = self._runner(
                    command_line,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=_DFU_TIMEOUT_SECONDS,
                    check=False,
                    env=environment,
                )
                output.seek(0, os.SEEK_END)
                length = output.tell()
                output.seek(max(0, length - _DFU_OUTPUT_BYTES))
                transcript = output.read().decode("utf-8", errors="replace").strip()
        except FileNotFoundError as err:
            raise FlashError("dfu-programmer is not available in the add-on image") from err
        except subprocess.TimeoutExpired as err:
            raise FlashError(f"dfu-programmer timed out while running {command}") from err
        except OSError as err:
            raise FlashError(f"could not run dfu-programmer {command}: {err}") from err
        if completed.returncode != 0:
            detail = transcript[-_DFU_OUTPUT_BYTES:] or "no diagnostic output"
            raise FlashError(f"dfu-programmer {command} failed ({completed.returncode}): {detail}")

    def _settle_after_usb_change(self, report: ProgressReporter, target: str) -> None:
        if not self._settings.qemu_usb_reenumeration_workaround:
            return
        report(25 if target == "USB DFU bootloader" else 84, f"Waiting for {target} USB re-enumeration.")
        self._sleep(_QEMU_SETTLE_SECONDS)

    @staticmethod
    def _status_error(known: KnownDevice | None, message: str) -> dict[str, object]:
        return {
            "state": "unavailable",
            "topology": known.topology if known else None,
            "usb_serial": known.usb_serial if known else None,
            "last_verified_version": known.version if known else None,
            "message": message,
        }
