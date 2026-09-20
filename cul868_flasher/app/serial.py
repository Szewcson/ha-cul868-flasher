"""Exclusive, minimal CUL CDC serial control without a third-party dependency."""

from __future__ import annotations

import errno
import fcntl
import os
import select
import termios
from pathlib import Path
from time import monotonic
from typing import Self


class CulSerialError(RuntimeError):
    """The CUL serial endpoint could not be safely controlled."""


_SPEEDS = {
    9_600: termios.B9600,
    19_200: termios.B19200,
    38_400: termios.B38400,
    57_600: termios.B57600,
    115_200: termios.B115200,
}
_READ_LIMIT = 2 * 1024
_VERSION_PREFIXES = (b"V ", b"VTS ")


def supports_culfw_led_control(version: str) -> bool:
    """Return whether a verified version has CULFW's documented LED command.

    The CULFW/a-culfw protocol uses ``V ... CUL868`` and documents ``l00``
    and ``l01`` for LED control. TSCULFW identifies itself with ``VTS ...``;
    it is deliberately excluded because this add-on has no verified command
    contract for its LED implementation.
    """

    return version.startswith("V ") and "CUL868" in version


class CulSerial:
    """Own a short exclusive CDC session and expose the required CUL commands."""

    def __init__(self, device: Path, baudrate: int, timeout: float = 3.0) -> None:
        self._device = device
        self._baudrate = baudrate
        self._timeout = timeout
        self._descriptor: int | None = None
        self._original_attributes: list[object] | None = None

    def __enter__(self) -> Self:
        if self._baudrate not in _SPEEDS:
            raise CulSerialError(f"unsupported CUL baudrate {self._baudrate}")
        try:
            descriptor = os.open(
                self._device,
                os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC,
            )
        except OSError as err:
            raise CulSerialError(f"cannot exclusively open CUL serial device {self._device}: {err}") from err
        self._descriptor = descriptor
        try:
            fcntl.ioctl(descriptor, termios.TIOCEXCL)
            self._original_attributes = termios.tcgetattr(descriptor)
            attributes = termios.tcgetattr(descriptor)
            attributes[0] = 0
            attributes[1] = 0
            attributes[2] &= ~(termios.PARENB | termios.CSTOPB | termios.CSIZE)
            attributes[2] |= termios.CLOCAL | termios.CREAD | termios.CS8
            attributes[3] = 0
            attributes[4] = _SPEEDS[self._baudrate]
            attributes[5] = _SPEEDS[self._baudrate]
            attributes[6][termios.VMIN] = 0
            attributes[6][termios.VTIME] = 0
            termios.tcsetattr(descriptor, termios.TCSANOW, attributes)
            termios.tcflush(descriptor, termios.TCIOFLUSH)
            return self
        except (OSError, termios.error) as err:
            self.__exit__(None, None, None)
            raise CulSerialError(f"cannot configure CUL serial device {self._device}: {err}") from err

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        del exc_type, exc_value, traceback
        descriptor = self._descriptor
        self._descriptor = None
        if descriptor is None:
            return
        try:
            if self._original_attributes is not None:
                termios.tcsetattr(descriptor, termios.TCSANOW, self._original_attributes)
            fcntl.ioctl(descriptor, termios.TIOCNXCL)
        except (OSError, termios.error):
            # A B01 command intentionally disconnects this device before cleanup.
            pass
        finally:
            self._original_attributes = None
            try:
                os.close(descriptor)
            except OSError:
                pass

    def version(self) -> str:
        """Read a bounded `V` response and prove that the application calls itself CUL868."""

        # TSCULFW reconnects its CDC device after reset and documents `V\r\n`
        # with a `VTS ...` response. CULFW and a-culfw retain the `V ...` form.
        response = self._request_version_line()
        if "CUL868" not in response:
            raise CulSerialError(
                "selected USB device did not identify itself as CUL868 in response to V"
            )
        return response

    def enter_bootloader(self) -> None:
        """Ask the verified application to persist its bootloader flag and reset."""

        self._write_all(b"B01\n")
        descriptor = self._require_descriptor()
        try:
            termios.tcdrain(descriptor)
        except (OSError, termios.error) as err:
            if getattr(err, "errno", None) not in {errno.EIO, errno.ENODEV}:
                raise CulSerialError(f"CUL bootloader command could not drain: {err}") from err

    def set_led(self, enabled: bool) -> None:
        """Send CULFW's documented LED command after the caller verified ``V``.

        CULFW does not echo commands, so draining the serial buffer only proves
        that the command reached the kernel; it is not a readback of LED state.
        """

        if not isinstance(enabled, bool):
            raise CulSerialError("CUL LED state must be a boolean")
        self._write_all(b"l01\r\n" if enabled else b"l00\r\n")
        descriptor = self._require_descriptor()
        try:
            termios.tcdrain(descriptor)
        except (OSError, termios.error) as err:
            raise CulSerialError(f"CUL LED command could not drain: {err}") from err

    def _request_version_line(self) -> str:
        """Return one supported CUL version line after the documented CRLF request."""

        self._write_all(b"V\r\n")
        descriptor = self._require_descriptor()
        deadline = monotonic() + self._timeout
        response = bytearray()
        while monotonic() < deadline:
            readable, _, _ = select.select([descriptor], [], [], max(0, deadline - monotonic()))
            if not readable:
                break
            try:
                chunk = os.read(descriptor, min(256, _READ_LIMIT - len(response)))
            except OSError as err:
                raise CulSerialError(f"CUL serial read failed: {err}") from err
            if not chunk:
                continue
            response.extend(chunk)
            for line in response.replace(b"\r", b"\n").split(b"\n"):
                if line.startswith(_VERSION_PREFIXES):
                    try:
                        return line.decode("ascii")
                    except UnicodeDecodeError as err:
                        raise CulSerialError("CUL version response is not ASCII") from err
            if len(response) >= _READ_LIMIT:
                raise CulSerialError("CUL version response exceeded the safe read limit")
        raise CulSerialError("CUL did not answer the V command before the timeout")

    def _write_all(self, data: bytes) -> None:
        descriptor = self._require_descriptor()
        view = memoryview(data)
        deadline = monotonic() + self._timeout
        while view:
            _, writable, _ = select.select([], [descriptor], [], max(0, deadline - monotonic()))
            if not writable:
                raise CulSerialError("CUL serial write timed out")
            try:
                written = os.write(descriptor, view)
            except OSError as err:
                raise CulSerialError(f"CUL serial write failed: {err}") from err
            if written <= 0:
                raise CulSerialError("CUL serial write made no progress")
            view = view[written:]

    def _require_descriptor(self) -> int:
        if self._descriptor is None:
            raise CulSerialError("CUL serial session is not open")
        return self._descriptor
