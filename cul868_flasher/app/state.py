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
RECOVERY_BINDING_VERIFIED_APPLICATION = "verified-application"
RECOVERY_BINDING_HANDOFF_PENDING = "handoff-pending"
RECOVERY_BINDING_OBSERVED_BOOTLOADER = "observed-bootloader"
RECOVERY_BINDING_LEGACY_UNKNOWN = "legacy-unknown"
_RECOVERY_BINDINGS = frozenset(
    {
        RECOVERY_BINDING_VERIFIED_APPLICATION,
        RECOVERY_BINDING_HANDOFF_PENDING,
        RECOVERY_BINDING_OBSERVED_BOOTLOADER,
        RECOVERY_BINDING_LEGACY_UNKNOWN,
    }
)


@dataclass(frozen=True)
class KnownDevice:
    """Non-secret CUL identity and the provenance of its recovery binding.

    An application and its DFU bootloader can publish different descriptor
    serials. A changed serial is therefore accepted only during the short,
    persisted ``handoff-pending`` interval immediately after ``B01``. Once a
    DFU descriptor has been observed, recovery binds to that descriptor rather
    than treating every unknown firmware state as an in-progress handoff.
    """

    topology: str
    usb_serial: str | None
    version: str | None
    configured_device: str | None = None
    recovery_binding: str = RECOVERY_BINDING_LEGACY_UNKNOWN
    handoff_deadline: int | None = None


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
        recovery_binding = _recovery_binding_for_document(document, version)
        if recovery_binding is None:
            return None
        handoff_deadline = _safe_handoff_deadline(document.get("handoff_deadline"))
        if recovery_binding == RECOVERY_BINDING_HANDOFF_PENDING and handoff_deadline is None:
            return None
        if recovery_binding != RECOVERY_BINDING_HANDOFF_PENDING:
            handoff_deadline = None
        return KnownDevice(
            topology,
            serial,
            version,
            configured_device,
            recovery_binding,
            handoff_deadline,
        )

    def save(self, device: KnownDevice) -> None:
        recovery_binding = _require_recovery_binding(device.recovery_binding)
        handoff_deadline = _safe_handoff_deadline(device.handoff_deadline)
        if recovery_binding == RECOVERY_BINDING_HANDOFF_PENDING and handoff_deadline is None:
            raise ValueError("handoff-pending recovery state requires a deadline")
        if recovery_binding != RECOVERY_BINDING_HANDOFF_PENDING:
            handoff_deadline = None
        payload = json.dumps(
            {
                "topology": validate_usb_topology(device.topology),
                "usb_serial": _safe_optional_text(device.usb_serial, 128),
                "version": _safe_optional_text(device.version, 512),
                "configured_device": _safe_optional_device_path(device.configured_device),
                "recovery_binding": recovery_binding,
                "handoff_deadline": handoff_deadline,
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


def _recovery_binding_for_document(
    document: dict[str, object], version: str | None
) -> str | None:
    """Migrate pre-phase records conservatively without silently widening trust."""

    if "recovery_binding" not in document:
        # Older verified application records can retain their strict serial
        # binding. Older unknown records cannot prove whether their serial was
        # observed before or after the USB personality changed.
        return (
            RECOVERY_BINDING_VERIFIED_APPLICATION
            if version is not None
            else RECOVERY_BINDING_LEGACY_UNKNOWN
        )
    value = document.get("recovery_binding")
    return value if isinstance(value, str) and value in _RECOVERY_BINDINGS else None


def _require_recovery_binding(value: object) -> str:
    if not isinstance(value, str) or value not in _RECOVERY_BINDINGS:
        raise ValueError("recovery binding is invalid")
    return value


def _safe_handoff_deadline(value: object) -> int | None:
    # Unix timestamps are only a bounded expiry marker, never an authorization
    # token. Reject booleans because bool is a subclass of int in Python.
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4_102_444_800:
        return None
    return value
