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
from typing import Protocol, Self

from .hexfile import HexImage, parse_hex_file
from .models import Settings
from .serial import CulSerial
from .state import DeviceStateStore, KnownDevice
from .supervisor import SupervisorClient
from .usb import ManualRecoveryTarget, UsbTarget, UsbTopology, UsbTopologyError

_DFU_EXECUTABLE = "/usr/local/bin/dfu-programmer"
_DFU_TIMEOUT_SECONDS = 60
_DFU_OUTPUT_BYTES = 4 * 1024
_POLL_SECONDS = 0.25
_QEMU_SETTLE_SECONDS = 8
_MIN_POST_DFU_TIMEOUT_SECONDS = 90
_VERSION_READ_ATTEMPTS = 3
_VERSION_RETRY_SECONDS = 1


class FlashError(RuntimeError):
    """The CUL868 update could not be completed safely."""


class DfuTransferError(FlashError):
    """dfu-programmer may have changed the target despite reporting failure."""


class SerialSession(Protocol):
    """The deliberately small serial surface required during a normal flash."""

    def __enter__(self) -> Self: ...

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
    manual_recovery: ManualRecoveryTarget | None = None


@dataclass(frozen=True)
class FlashPreflight:
    """A resolved flash target plus any required one-time recovery consent."""

    mode: str
    topology: str
    message: str
    manual_recovery: ManualRecoveryTarget | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "topology": self.topology,
            "message": self.message,
            "requires_unpaired_recovery_confirmation": self.manual_recovery is not None,
        }


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
            try:
                manual_recovery = self._single_unpaired_recovery_target()
            except FlashError as recovery_error:
                return self._status_error(
                    known_for_config, f"{application_error}. {recovery_error}"
                )
            if manual_recovery is not None:
                return {
                    "state": "unpaired_bootloader",
                    "topology": manual_recovery.topology,
                    "usb_serial": manual_recovery.usb_serial,
                    "last_verified_version": None,
                    "message": (
                        "One unpaired CUL868 DFU bootloader is present. Validate firmware, then "
                        "explicitly confirm that this is the configured CUL868 before recovery."
                    ),
                }
            return self._status_error(known_for_config, str(application_error))
        return {
            "state": "application",
            "topology": application.topology,
            "usb_serial": application.usb_serial,
            "last_verified_version": self._known_version_for_application(known_for_config, application),
            "message": "Configured CUL application USB endpoint is present.",
        }

    def preflight(self) -> FlashPreflight:
        """Resolve a safe target before accepting a confirmation to flash it."""

        plan = self._plan(allow_unpaired_recovery=True)
        if plan.mode == "application":
            return FlashPreflight(
                "application",
                plan.topology,
                (
                    f"Configured CUL endpoint found at USB path {plan.topology}. "
                    "It will be verified with V before flashing."
                ),
            )
        if plan.manual_recovery is not None:
            return FlashPreflight(
                "manual-recovery",
                plan.topology,
                (
                    f"One unpaired CUL868 DFU bootloader was found at USB path {plan.topology}. "
                    "A second explicit confirmation is required before this USB path can be flashed."
                ),
                plan.manual_recovery,
            )
        return FlashPreflight(
            "recovery",
            plan.topology,
            (
                f"Known CUL868 USB DFU bootloader found at USB path {plan.topology}. "
                "Recovery will flash this same USB path."
            ),
        )

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

    def flash(
        self,
        image: HexImage,
        report: ProgressReporter,
        *,
        manual_recovery: ManualRecoveryTarget | None = None,
    ) -> dict[str, object]:
        """Execute one non-interruptible verified-image DFU transaction.

        A process-local lock is a final guard in addition to the HTTP operation
        queue. It prevents future callers from accidentally racing raw USB DFU.
        """

        with self._flash_lock:
            image = self._revalidate_image(image)
            report(3, "Resolving the configured CUL868 USB target.")
            plan = self._plan(manual_recovery=manual_recovery)
            expected_usb_serial = self._expected_usb_serial(plan)
            allow_qemu_topology_change = self._settings.qemu_usb_reenumeration_workaround
            with self._supervisor.temporarily_stop_wmbusmeters(self._settings.device) as pause:
                if plan.mode == "application":
                    before = self._enter_bootloader(
                        plan,
                        report,
                        lambda: self._begin_uncertain_transition(
                            plan, expected_usb_serial, pause.leave_stopped_after_error
                        ),
                    )
                    self._settle_after_usb_change(report, "USB DFU bootloader")
                    bootloader = self._wait_for_bootloader(
                        plan.topology,
                        expected_usb_serial,
                        self._post_dfu_timeout(),
                        allow_qemu_topology_change=allow_qemu_topology_change,
                    )
                else:
                    before = plan.known.version if plan.known else None
                    if plan.manual_recovery is not None:
                        report(15, "Using the explicitly confirmed CUL868 USB DFU bootloader.")
                        pause.leave_stopped_after_error()
                    else:
                        report(15, "Using the saved CUL868 USB path in DFU recovery mode.")
                        self._begin_uncertain_transition(
                            plan, expected_usb_serial, pause.leave_stopped_after_error
                        )
                    bootloader = self._wait_for_bootloader(
                        plan.topology,
                        expected_usb_serial,
                        allow_qemu_topology_change=allow_qemu_topology_change,
                    )

                dfu_topology = bootloader.topology
                if dfu_topology != plan.topology:
                    report(
                        30,
                        f"QEMU reattached the CUL DFU bootloader on guest USB path {dfu_topology}.",
                    )
                    self._store_unverified(
                        plan,
                        bootloader.usb_serial or expected_usb_serial,
                        topology=dfu_topology,
                    )

                if plan.manual_recovery is not None:
                    # The explicit recovery confirmation authorizes this new
                    # binding only once erase may change the target.
                    self._store_unverified(
                        plan,
                        bootloader.usb_serial or expected_usb_serial,
                        topology=dfu_topology,
                    )
                report(35, "Running the standard CUL DFU erase command.")
                self._run_dfu(dfu_topology, expected_usb_serial, "erase")
                report(55, "Writing the validated firmware image.")
                self._run_dfu(dfu_topology, expected_usb_serial, "flash", str(image.path))
                report(78, "Starting the new CUL868 firmware.")
                start_error: DfuTransferError | None = None
                try:
                    self._run_dfu(dfu_topology, expected_usb_serial, "start")
                except DfuTransferError as err:
                    # The DFU transport can disappear after accepting start.
                    # A live CUL V response is the only success evidence.
                    start_error = err
                    report(82, "CUL DFU start reported an error; checking the application.")
                self._settle_after_usb_change(report, "CUL868 application")

                try:
                    application, device = self._wait_for_application(
                        dfu_topology,
                        expected_usb_serial,
                        self._post_dfu_timeout(),
                        allow_qemu_topology_change=allow_qemu_topology_change,
                    )
                    if application.topology != dfu_topology:
                        report(
                            88,
                            f"QEMU reattached the CUL application on guest USB path "
                            f"{application.topology}.",
                        )
                    report(90, "Verifying the restarted CUL868 firmware.")
                    after = self._read_version(device)
                except FlashError as err:
                    if start_error is not None:
                        raise FlashError(
                            f"{start_error}; CUL did not verify the application after DFU: {err}"
                        ) from start_error
                    raise
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
                "topology": application.topology,
                "paused_wmbusmeters_addons": list(pause.addons),
                "firmware_sha256": image.sha256,
                "firmware_bytes": image.data_bytes,
            }

    @staticmethod
    def _expected_usb_serial(plan: _FlashPlan) -> str | None:
        if plan.application is not None:
            return plan.application.usb_serial
        if plan.bootloader is not None:
            return plan.bootloader.usb_serial
        return plan.known.usb_serial if plan.known is not None else None

    def _store_unverified(
        self,
        plan: _FlashPlan,
        usb_serial: str | None,
        *,
        topology: str | None = None,
    ) -> None:
        """Persist only the safe recovery binding after an uncertain transition."""

        self._state.save(
            KnownDevice(topology or plan.topology, usb_serial, None, str(self._settings.device))
        )

    def _begin_uncertain_transition(
        self,
        plan: _FlashPlan,
        usb_serial: str | None,
        leave_stopped_after_error: Callable[[], None],
    ) -> None:
        """Preserve recovery state before a CUL can leave its normal application."""

        self._store_unverified(plan, usb_serial)
        leave_stopped_after_error()

    def _post_dfu_timeout(self) -> int:
        """Leave enough time for a USB personality change on a virtualized host."""

        return max(self._settings.boot_timeout, _MIN_POST_DFU_TIMEOUT_SECONDS)

    def _plan(
        self,
        *,
        manual_recovery: ManualRecoveryTarget | None = None,
        allow_unpaired_recovery: bool = False,
    ) -> _FlashPlan:
        known = self._state.load()
        known_for_config = known if self._state_matches_configuration(known) else None
        try:
            application = self._topology.configured_application(self._settings.device)
        except UsbTopologyError as application_error:
            if known_for_config is not None:
                try:
                    bootloader = self._bootloader_for_known(known_for_config)
                except FlashError as bootloader_error:
                    raise FlashError(str(bootloader_error)) from bootloader_error
                if bootloader is not None:
                    return _FlashPlan(
                        "recovery", known_for_config.topology, None, bootloader, known_for_config
                    )
            if manual_recovery is not None:
                bootloader = self._manual_bootloader_for_target(manual_recovery)
                return _FlashPlan(
                    "manual-recovery",
                    manual_recovery.topology,
                    None,
                    bootloader,
                    None,
                    manual_recovery,
                )
            if allow_unpaired_recovery:
                discovered = self._single_unpaired_recovery_target()
                if discovered is not None:
                    bootloader = self._manual_bootloader_for_target(discovered)
                    return _FlashPlan(
                        "manual-recovery",
                        discovered.topology,
                        None,
                        bootloader,
                        None,
                        discovered,
                    )
            if known is not None and known_for_config is None:
                raise FlashError(
                    "configured CUL serial path changed since the last verified device; "
                    "automatic bootloader recovery is refused"
                ) from application_error
            unpaired = self._single_unpaired_recovery_target()
            if unpaired is not None:
                raise FlashError(
                    "unpaired CUL868 DFU recovery requires explicit confirmation during firmware validation"
                )
            raise FlashError(
                f"configured CUL application is unavailable: {application_error}. "
                "No uniquely identifiable CUL868 DFU bootloader is available for recovery."
            ) from application_error
        if manual_recovery is not None:
            raise FlashError(
                "the explicitly confirmed DFU bootloader is no longer active; validate the firmware again"
            )
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

    def _single_unpaired_recovery_target(self) -> ManualRecoveryTarget | None:
        """Offer recovery only when one expected bootloader is unambiguous."""

        try:
            bootloaders = self._topology.bootloader_targets()
        except UsbTopologyError as err:
            raise FlashError(f"could not enumerate CUL868 DFU bootloaders: {err}") from err
        if not bootloaders:
            return None
        if len(bootloaders) != 1:
            paths = ", ".join(target.topology for target in bootloaders[:4])
            raise FlashError(
                f"found {len(bootloaders)} CUL868 DFU bootloaders ({paths}); "
                "unplug or detach the others before explicit recovery"
            )
        target = bootloaders[0]
        return ManualRecoveryTarget(target.topology, target.usb_serial)

    def _manual_bootloader_for_target(self, expected: ManualRecoveryTarget) -> UsbTarget:
        """Re-prove the server-side selected bootloader immediately before use."""

        try:
            bootloader = self._topology.bootloader_for_topology(expected.topology)
        except UsbTopologyError as err:
            raise FlashError(f"could not inspect the confirmed CUL868 DFU target: {err}") from err
        if bootloader is None:
            raise FlashError(
                f"the confirmed CUL868 DFU bootloader is no longer present on USB path "
                f"{expected.topology}"
            )
        if expected.usb_serial is not None and bootloader.usb_serial != expected.usb_serial:
            raise FlashError(
                "the USB serial number for the confirmed CUL868 DFU bootloader changed; "
                "validate the firmware again"
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

    def _enter_bootloader(
        self,
        plan: _FlashPlan,
        report: ProgressReporter,
        begin_uncertain_transition: Callable[[], None],
    ) -> str:
        if plan.application is None:
            raise FlashError("internal error: application flash plan has no USB target")
        report(10, "Verifying that the selected USB serial device is a CUL868.")
        last_error: Exception | None = None
        for attempt in range(1, _VERSION_READ_ATTEMPTS + 1):
            bootloader_requested = False
            try:
                with self._serial_factory(self._settings.device, self._settings.baudrate) as serial:
                    before = serial.version()
                    report(20, "Requesting the verified CUL868 application to enter USB DFU mode.")
                    begin_uncertain_transition()
                    # Do not retry after this point: B01 can take effect even
                    # when the serial transport reports a disconnect.
                    bootloader_requested = True
                    serial.enter_bootloader()
            except Exception as err:
                if bootloader_requested:
                    raise FlashError(
                        f"could not complete the CUL bootloader handoff: {err}"
                    ) from err
                last_error = err
                if attempt != _VERSION_READ_ATTEMPTS:
                    self._sleep(_VERSION_RETRY_SECONDS)
                    continue
                break
            return before
        assert last_error is not None
        raise FlashError(
            f"could not verify the selected CUL868 after {_VERSION_READ_ATTEMPTS} attempts: {last_error}"
        ) from last_error

    def _wait_for_bootloader(
        self,
        topology: str,
        expected_usb_serial: str | None = None,
        timeout: int | None = None,
        *,
        allow_qemu_topology_change: bool = False,
    ) -> UsbTarget:
        effective_timeout = self._settings.boot_timeout if timeout is None else timeout
        deadline = self._monotonic() + effective_timeout
        last_error = "DFU bootloader has not appeared"
        while self._monotonic() < deadline:
            try:
                bootloader = self._topology.bootloader_for_topology(topology)
            except UsbTopologyError as err:
                last_error = str(err)
            else:
                if bootloader is not None:
                    self._require_expected_usb_serial(
                        bootloader, expected_usb_serial, "DFU device on the selected USB path"
                    )
                    return bootloader
                if allow_qemu_topology_change:
                    reenumerated = self._single_reenumerated_target(
                        self._topology.bootloader_targets,
                        expected_usb_serial,
                        "CUL868 DFU bootloader",
                    )
                    if reenumerated is not None:
                        return reenumerated
            self._sleep(_POLL_SECONDS)
        if not allow_qemu_topology_change:
            last_error = self._qemu_topology_hint(
                self._topology.bootloader_targets,
                topology,
                "CUL868 DFU bootloader",
                last_error,
            )
        raise FlashError(
            f"CUL868 DFU bootloader 03eb:2ff4 did not appear on USB path {topology}: {last_error}"
        )

    def _wait_for_application(
        self,
        topology: str,
        expected_usb_serial: str | None = None,
        timeout: int | None = None,
        *,
        allow_qemu_topology_change: bool = False,
    ) -> tuple[UsbTarget, Path]:
        effective_timeout = self._settings.boot_timeout if timeout is None else timeout
        deadline = self._monotonic() + effective_timeout
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
                if allow_qemu_topology_change:
                    reenumerated = self._single_reenumerated_target(
                        self._topology.application_targets,
                        expected_usb_serial,
                        "CUL868 application",
                    )
                    if reenumerated is not None:
                        try:
                            device = self._topology.tty_for_topology(reenumerated.topology)
                        except UsbTopologyError as err:
                            last_error = str(err)
                        else:
                            if device is not None:
                                return reenumerated, device
            self._sleep(_POLL_SECONDS)
        if not allow_qemu_topology_change:
            last_error = self._qemu_topology_hint(
                self._topology.application_targets,
                topology,
                "CUL868 application",
                last_error,
            )
        raise FlashError(
            f"CUL868 application 03eb:204b did not return on USB path {topology}: {last_error}"
        )

    def _single_reenumerated_target(
        self,
        discovery: Callable[[], tuple[UsbTarget, ...]],
        expected_usb_serial: str | None,
        label: str,
    ) -> UsbTarget | None:
        """Accept a QEMU-moved target only when it is unique and still matches."""

        try:
            targets = discovery()
        except UsbTopologyError as err:
            raise FlashError(f"could not inspect {label} after QEMU re-enumeration: {err}") from err
        if not targets:
            return None
        if len(targets) != 1:
            paths = ", ".join(target.topology for target in targets[:4])
            raise FlashError(
                f"found {len(targets)} {label} targets during QEMU guest USB re-enumeration "
                f"({paths}); refusing to choose one"
            )
        target = targets[0]
        self._require_expected_usb_serial(
            target, expected_usb_serial, f"{label} on changed guest USB path"
        )
        return target

    @staticmethod
    def _require_expected_usb_serial(
        target: UsbTarget, expected_usb_serial: str | None, label: str
    ) -> None:
        if (
            expected_usb_serial is not None
            and target.usb_serial is not None
            and target.usb_serial != expected_usb_serial
        ):
            raise FlashError(f"{label} has a different USB serial number")

    def _qemu_topology_hint(
        self,
        discovery: Callable[[], tuple[UsbTarget, ...]],
        expected_topology: str,
        label: str,
        last_error: str,
    ) -> str:
        """Explain a visible QEMU port reassignment without weakening the default."""

        try:
            targets = discovery()
        except UsbTopologyError:
            return last_error
        if len(targets) == 1 and targets[0].topology != expected_topology:
            return (
                f"{label} is visible on guest USB path {targets[0].topology}, not {expected_topology}; "
                "enable QEMU USB re-enumeration workaround to permit this exact-one fallback"
            )
        return last_error

    def _read_version(self, device: Path) -> str:
        last_error: Exception | None = None
        for attempt in range(1, _VERSION_READ_ATTEMPTS + 1):
            try:
                with self._serial_factory(device, self._settings.baudrate) as serial:
                    return serial.version()
            # A fake or platform-specific serial adapter can surface diverse
            # transport errors; retry regular exceptions but never BaseException.
            except Exception as err:  # noqa: BLE001
                last_error = err
                if attempt != _VERSION_READ_ATTEMPTS:
                    self._sleep(_VERSION_RETRY_SECONDS)
        assert last_error is not None
        raise FlashError(
            f"CUL868 did not provide a valid V response after {_VERSION_READ_ATTEMPTS} attempts: {last_error}"
        ) from last_error

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
            raise DfuTransferError(f"dfu-programmer timed out while running {command}") from err
        except OSError as err:
            raise DfuTransferError(f"could not run dfu-programmer {command}: {err}") from err
        if completed.returncode != 0:
            detail = transcript[-_DFU_OUTPUT_BYTES:] or "no diagnostic output"
            raise DfuTransferError(
                f"dfu-programmer {command} failed ({completed.returncode}): {detail}"
            )

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
