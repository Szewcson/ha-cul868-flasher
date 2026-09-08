"""Small deterministic fakes shared by the hardware-orchestration tests."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.usb import (
    APPLICATION_PRODUCT_ID,
    APPLICATION_VENDOR_ID,
    BOOTLOADER_PRODUCT_ID,
    BOOTLOADER_VENDOR_ID,
    UsbTarget,
    UsbTopologyError,
)


def intel_hex_record(address: int, record_type: int, data: bytes = b"") -> str:
    """Create one checksum-correct Intel HEX record for tests."""

    fields = bytes([len(data), address >> 8, address & 0xFF, record_type]) + data
    checksum = (-sum(fields)) & 0xFF
    return ":" + (fields + bytes([checksum])).hex().upper()


def minimal_hex() -> bytes:
    return (intel_hex_record(0, 0, b"\x0c\x94\x00\x00") + "\n" + intel_hex_record(0, 1) + "\n").encode()


def target(
    *,
    topology: str = "2-3",
    bootloader: bool = False,
    serial: str | None = "CUL-TEST",
    address: int = 7,
) -> UsbTarget:
    return UsbTarget(
        topology=topology,
        vendor_id=BOOTLOADER_VENDOR_ID if bootloader else APPLICATION_VENDOR_ID,
        product_id=BOOTLOADER_PRODUCT_ID if bootloader else APPLICATION_PRODUCT_ID,
        bus_number=2,
        device_address=address,
        usb_serial=serial,
        product="CUL868" if not bootloader else "ATmega32U4 DFU",
        sysfs_path=Path(f"/sys/bus/usb/devices/{topology}"),
    )


class FakeTopology:
    """A device whose USB personality changes only when the test requests it."""

    def __init__(self, mode: str = "application", serial: str | None = "CUL-TEST") -> None:
        self.mode = mode
        self.application = target(serial=serial)
        self.bootloader = target(bootloader=True, serial=serial)

    def configured_application(self, _device: Path) -> UsbTarget:
        if self.mode != "application":
            raise UsbTopologyError("configured CUL serial device is not present")
        return self.application

    def application_for_topology(self, topology: str) -> UsbTarget | None:
        return self.application if self.mode == "application" and topology == "2-3" else None

    def bootloader_for_topology(self, topology: str) -> UsbTarget | None:
        return self.bootloader if self.mode == "bootloader" and topology == "2-3" else None

    def tty_for_topology(self, topology: str) -> Path | None:
        if self.mode == "application" and topology == "2-3":
            return Path("/dev/ttyACM0")
        return None


class FakeSerial:
    def __init__(self, topology: FakeTopology, versions: list[str]) -> None:
        self._topology = topology
        self._versions = versions
        self.entered_bootloader = False

    def __enter__(self) -> FakeSerial:
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        return None

    def version(self) -> str:
        return self._versions.pop(0)

    def enter_bootloader(self) -> None:
        self.entered_bootloader = True
        self._topology.mode = "bootloader"


class FakeSerialFactory:
    def __init__(self, topology: FakeTopology, versions: list[str]) -> None:
        self._topology = topology
        self._versions = versions
        self.calls: list[tuple[Path, int]] = []
        self.sessions: list[FakeSerial] = []

    def __call__(self, device: Path, baudrate: int) -> FakeSerial:
        self.calls.append((device, baudrate))
        session = FakeSerial(self._topology, self._versions)
        self.sessions.append(session)
        return session


class FakeSupervisor:
    def __init__(self, paused: tuple[str, ...] = ("wmbusmeters",)) -> None:
        self.paused = paused
        self.events: list[str] = []

    @contextmanager
    def temporarily_stop_wmbusmeters(self, _device: Path) -> Iterator[tuple[str, ...]]:
        self.events.append("stop")
        try:
            yield self.paused
        finally:
            self.events.append("start")
