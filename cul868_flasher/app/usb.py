"""Linux USB topology discovery for one CUL868 V3 device and its DFU mode."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

APPLICATION_VENDOR_ID = "03eb"
APPLICATION_PRODUCT_ID = "204b"
BOOTLOADER_VENDOR_ID = "03eb"
BOOTLOADER_PRODUCT_ID = "2ff4"
_TOPOLOGY_RE = re.compile(r"^[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*)*$")
_USB_ID_RE = re.compile(r"^[0-9a-f]{4}$")


class UsbTopologyError(RuntimeError):
    """The selected serial device cannot be bound to one safe USB target."""


@dataclass(frozen=True)
class UsbTarget:
    """One USB device identified by Linux's current bus/device address and port."""

    topology: str
    vendor_id: str
    product_id: str
    bus_number: int
    device_address: int
    usb_serial: str | None
    product: str | None
    sysfs_path: Path

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id}:{self.product_id}"


@dataclass(frozen=True)
class ManualRecoveryTarget:
    """A bootloader-only target selected by an explicit operator confirmation."""

    topology: str
    usb_serial: str | None

    def __post_init__(self) -> None:
        validate_usb_topology(self.topology)
        if self.usb_serial is not None and (
            not isinstance(self.usb_serial, str)
            or not self.usb_serial
            or len(self.usb_serial) > 128
            or not self.usb_serial.isascii()
            or any(ord(character) < 32 or ord(character) > 126 for character in self.usb_serial)
        ):
            raise ValueError("USB serial is invalid")


def validate_usb_topology(value: object) -> str:
    if not isinstance(value, str) or not _TOPOLOGY_RE.fullmatch(value):
        raise ValueError("USB topology must look like 2-3 or 2-3.1")
    return value


class UsbTopology:
    """Resolve CDC nodes and raw-USB DFU devices through a common USB port."""

    def __init__(
        self,
        *,
        sys_class_tty: Path = Path("/sys/class/tty"),
        sys_bus_usb: Path = Path("/sys/bus/usb/devices"),
        dev_directory: Path = Path("/dev"),
    ) -> None:
        self._sys_class_tty = sys_class_tty
        self._sys_bus_usb = sys_bus_usb
        self._dev_directory = dev_directory

    def configured_application(self, device: Path) -> UsbTarget:
        """Return the selected normal CUL application, never an arbitrary TTY."""

        try:
            resolved = device.resolve(strict=True)
        except OSError as err:
            raise UsbTopologyError(f"configured CUL serial device {device} is not present") from err
        if resolved.parent != self._dev_directory or not resolved.name.startswith(("ttyACM", "ttyUSB")):
            raise UsbTopologyError(f"configured device {device} is not a supported USB serial endpoint")
        target = self._target_for_tty(resolved.name)
        self._require_identity(target, APPLICATION_VENDOR_ID, APPLICATION_PRODUCT_ID, "CUL application")
        return target

    def application_for_topology(self, topology: str) -> UsbTarget | None:
        return self._single_target(
            topology,
            APPLICATION_VENDOR_ID,
            APPLICATION_PRODUCT_ID,
            "CUL application",
        )

    def application_targets(self) -> tuple[UsbTarget, ...]:
        """List unique normal CUL applications for a QEMU re-enumeration check."""

        return self._targets_with_identity(APPLICATION_VENDOR_ID, APPLICATION_PRODUCT_ID)

    def bootloader_for_topology(self, topology: str) -> UsbTarget | None:
        return self._single_target(
            topology,
            BOOTLOADER_VENDOR_ID,
            BOOTLOADER_PRODUCT_ID,
            "ATmega32U4 DFU bootloader",
        )

    def bootloader_targets(self) -> tuple[UsbTarget, ...]:
        """List unique expected bootloaders for an explicitly confirmed recovery.

        The caller must still require an operator confirmation before using this
        list.  Deduplicating sysfs aliases avoids treating one physical device
        as ambiguous, but different physical USB paths always remain distinct.
        """

        return self._targets_with_identity(BOOTLOADER_VENDOR_ID, BOOTLOADER_PRODUCT_ID)

    def tty_for_topology(self, topology: str) -> Path | None:
        """Find the current CDC node for the exact application USB port."""

        try:
            entries = tuple(self._sys_class_tty.iterdir())
        except OSError:
            return None
        matches: list[Path] = []
        for entry in entries:
            try:
                target = self._target_for_tty(entry.name)
            except UsbTopologyError:
                continue
            if (
                target.topology == topology
                and target.vendor_id == APPLICATION_VENDOR_ID
                and target.product_id == APPLICATION_PRODUCT_ID
            ):
                matches.append(self._dev_directory / entry.name)
        if len(matches) > 1:
            raise UsbTopologyError(
                f"more than one serial endpoint belongs to CUL USB path {topology}"
            )
        return matches[0] if matches else None

    def _single_target(
        self, topology: str, vendor_id: str, product_id: str, label: str
    ) -> UsbTarget | None:
        topology = validate_usb_topology(topology)
        matches = [
            target
            for target in self._all_usb_targets()
            if target.topology == topology
            and target.vendor_id == vendor_id
            and target.product_id == product_id
        ]
        if len(matches) > 1:
            raise UsbTopologyError(f"more than one {label} is present at USB path {topology}")
        return matches[0] if matches else None

    def _targets_with_identity(self, vendor_id: str, product_id: str) -> tuple[UsbTarget, ...]:
        """Return physical USB targets once, despite duplicate sysfs aliases."""

        targets: dict[Path, UsbTarget] = {}
        for target in self._all_usb_targets():
            if target.vendor_id != vendor_id or target.product_id != product_id:
                continue
            try:
                identity = target.sysfs_path.resolve(strict=True)
            except OSError:
                identity = target.sysfs_path
            targets.setdefault(identity, target)
        return tuple(sorted(targets.values(), key=lambda target: target.topology))

    def _target_for_tty(self, tty_name: str) -> UsbTarget:
        if not tty_name.startswith(("ttyACM", "ttyUSB")):
            raise UsbTopologyError(f"{tty_name} is not a USB CDC serial endpoint")
        try:
            device_path = (self._sys_class_tty / tty_name / "device").resolve(strict=True)
        except OSError as err:
            raise UsbTopologyError(f"could not resolve USB topology for {tty_name}") from err
        return self._target_from_descendant(device_path)

    def _all_usb_targets(self) -> tuple[UsbTarget, ...]:
        try:
            entries = tuple(self._sys_bus_usb.iterdir())
        except OSError as err:
            raise UsbTopologyError("USB topology is unavailable inside this add-on") from err
        targets: list[UsbTarget] = []
        for path in entries:
            try:
                target = self._target_from_path(path)
            except UsbTopologyError:
                continue
            targets.append(target)
        return tuple(targets)

    def _target_from_descendant(self, path: Path) -> UsbTarget:
        current = path
        while True:
            try:
                return self._target_from_path(current)
            except UsbTopologyError:
                parent = current.parent
                if parent == current:
                    break
                current = parent
        raise UsbTopologyError(f"could not find a USB device ancestor for {path}")

    def _target_from_path(self, path: Path) -> UsbTarget:
        vendor_id = _read_attribute(path / "idVendor")
        product_id = _read_attribute(path / "idProduct")
        if vendor_id is None or product_id is None:
            raise UsbTopologyError(f"{path.name} is not a USB device")
        vendor_id = vendor_id.lower()
        product_id = product_id.lower()
        if not _USB_ID_RE.fullmatch(vendor_id) or not _USB_ID_RE.fullmatch(product_id):
            raise UsbTopologyError(f"{path.name} has malformed USB identifiers")
        try:
            topology = validate_usb_topology(path.name)
            bus_number = _positive_integer(path / "busnum")
            device_address = _positive_integer(path / "devnum")
        except ValueError as err:
            raise UsbTopologyError(f"{path.name} has incomplete USB topology data") from err
        return UsbTarget(
            topology=topology,
            vendor_id=vendor_id,
            product_id=product_id,
            bus_number=bus_number,
            device_address=device_address,
            usb_serial=_optional_text(path / "serial", 128),
            product=_optional_text(path / "product", 128),
            sysfs_path=path,
        )

    @staticmethod
    def _require_identity(target: UsbTarget, vendor_id: str, product_id: str, label: str) -> None:
        if target.vendor_id != vendor_id or target.product_id != product_id:
            raise UsbTopologyError(
                f"selected serial device is {target.vid_pid}, not the expected {label} "
                f"{vendor_id}:{product_id}"
            )


def _read_attribute(path: Path) -> str | None:
    """Read a small sysfs attribute without trusting its synthetic ``st_size``.

    Linux sysfs often reports a page-sized ``st_size`` for tiny attributes
    such as ``idVendor``. Read a fixed bounded amount instead of rejecting a
    valid USB device based on that metadata.
    """

    try:
        with path.open("rb") as source:
            raw = source.read(1025)
    except OSError:
        return None
    if len(raw) > 1024:
        return None
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return None
    return value if value else None


def _optional_text(path: Path, maximum: int) -> str | None:
    value = _read_attribute(path)
    if value is None or len(value) > maximum or not value.isascii():
        return None
    if any(ord(character) < 32 or ord(character) > 126 for character in value):
        return None
    return value


def _positive_integer(path: Path) -> int:
    value = _read_attribute(path)
    if value is None or not value.isdecimal():
        raise ValueError(path.name)
    parsed = int(value)
    if parsed < 1:
        raise ValueError(path.name)
    return parsed
