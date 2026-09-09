"""Small atomic state record used only to recover a known CUL868 bootloader."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Lock

from .usb import validate_usb_topology

_MAX_STATE_BYTES = 4 * 1024
_STATE_FILENAME = "cul868-flasher-state.json"


@dataclass(frozen=True)
class KnownDevice:
    """Non-secret identity of the last verified normal CUL868 USB topology."""

    topology: str
    usb_serial: str | None
    version: str | None
    configured_device: str | None = None


class DeviceStateStore:
    """Serialize state reads and atomic replacements across Ingress and worker threads."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._path = directory / _STATE_FILENAME
        self._lock = Lock()

    def load(self) -> KnownDevice | None:
        with self._lock:
            try:
                if self._path.stat().st_size > _MAX_STATE_BYTES:
                    return None
                document = json.loads(self._path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                return None
        if not isinstance(document, dict):
            return None
        try:
            topology = validate_usb_topology(document.get("topology"))
        except ValueError:
            return None
        serial = _safe_optional_text(document.get("usb_serial"), 128)
        version = _safe_optional_text(document.get("version"), 512)
        configured_device = _safe_optional_device_path(document.get("configured_device"))
        return KnownDevice(topology, serial, version, configured_device)

    def save(self, device: KnownDevice) -> None:
        payload = json.dumps(
            {
                "topology": validate_usb_topology(device.topology),
                "usb_serial": _safe_optional_text(device.usb_serial, 128),
                "version": _safe_optional_text(device.version, 512),
                "configured_device": _safe_optional_device_path(device.configured_device),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        with self._lock:
            self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".cul868-state-", dir=self._directory
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                os.chmod(temporary_path, 0o600)
                os.replace(temporary_path, self._path)
            finally:
                temporary_path.unlink(missing_ok=True)


def _safe_optional_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        return None
    if not value.isascii() or any(ord(character) < 32 or ord(character) > 126 for character in value):
        return None
    return value


def _safe_optional_device_path(value: object) -> str | None:
    """Persist the selected path only as a recovery-safety binding."""

    if not isinstance(value, str) or not value or len(value) > 512 or not value.isascii():
        return None
    path = PurePosixPath(value)
    if not path.is_absolute() or path.parts[:2] != ("/", "dev") or ".." in path.parts:
        return None
    return value
