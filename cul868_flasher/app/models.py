"""Validated configuration for the narrowly scoped CUL868 V3 flasher."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class ValidationError(ValueError):
    """A Supervisor option is malformed or unsafe to use."""


SUPPORTED_BAUDRATES = frozenset({9_600, 19_200, 38_400, 57_600, 115_200})
DEFAULT_BAUDRATE = 9_600
DEFAULT_BOOT_TIMEOUT = 90
DEFAULT_QEMU_USB_REENUMERATION_WORKAROUND = False
MAX_ADDITIONAL_CUL_ADDONS = 16


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _optional_bool(value: object, name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValidationError(f"{name} must be a boolean")
    return value


def _integer(value: object, name: str, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{name} must be an integer")
    return value


def _device_path(value: object) -> Path:
    raw = _required_string(value, "device")
    if len(raw) > 512 or "\x00" in raw:
        raise ValidationError("device path is invalid")
    normalized = PurePosixPath(raw)
    if not normalized.is_absolute() or normalized.parts[:2] != ("/", "dev"):
        raise ValidationError("device must be a Home Assistant mapped /dev path")
    if ".." in normalized.parts or len(normalized.parts) < 3:
        raise ValidationError("device path is invalid")
    return Path(raw)


def _additional_cul_addons(value: object) -> tuple[str, ...]:
    """Validate explicit lifecycle opt-ins for services with private CUL config.

    Supervisor names third-party apps with a lowercase repository prefix and
    slug. Restrict the option to that syntax so it can never form a path or an
    arbitrary Supervisor endpoint.
    """

    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_ADDITIONAL_CUL_ADDONS:
        raise ValidationError(
            f"additional_cul_addons must be a list of at most {MAX_ADDITIONAL_CUL_ADDONS} app slugs"
        )
    slugs: list[str] = []
    for value_item in value:
        if (
            not isinstance(value_item, str)
            or not 1 <= len(value_item) <= 128
            or value_item == "self"
            or any(
                character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in value_item
            )
        ):
            raise ValidationError("additional_cul_addons contains an invalid app slug")
        slugs.append(value_item)
    return tuple(sorted(set(slugs)))


@dataclass(frozen=True)
class Settings:
    """Runtime settings delivered by Home Assistant Supervisor."""

    device: Path
    baudrate: int
    boot_timeout: int
    qemu_usb_reenumeration_workaround: bool
    additional_cul_addons: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, options: dict[str, object]) -> Settings:
        device = _device_path(options.get("device"))

        baudrate = options.get("baudrate", DEFAULT_BAUDRATE)
        if isinstance(baudrate, str) and baudrate.isascii() and baudrate.isdecimal():
            baudrate = int(baudrate)
        if isinstance(baudrate, bool) or not isinstance(baudrate, int):
            raise ValidationError("baudrate must be an integer")
        if baudrate not in SUPPORTED_BAUDRATES:
            raise ValidationError("baudrate is not supported by this app")

        boot_timeout = _integer(options.get("boot_timeout"), "boot_timeout", DEFAULT_BOOT_TIMEOUT)
        if not 15 <= boot_timeout <= 120:
            raise ValidationError("boot_timeout must be between 15 and 120 seconds")

        return cls(
            device=device,
            baudrate=baudrate,
            boot_timeout=boot_timeout,
            qemu_usb_reenumeration_workaround=_optional_bool(
                options.get("qemu_usb_reenumeration_workaround"),
                "qemu_usb_reenumeration_workaround",
                DEFAULT_QEMU_USB_REENUMERATION_WORKAROUND,
            ),
            additional_cul_addons=_additional_cul_addons(options.get("additional_cul_addons")),
        )
